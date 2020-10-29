""" sync repo script
"""
import sys
import concurrent.futures
import click
import sh
import os
import uuid
import yaml
from pathlib import Path
from urllib.parse import urlparse, quote
from sh.contrib import git
from cilib.run import capture, cmd_ok
from cilib import log, enums
from cilib.models.repos.kubernetes import (
    UpstreamKubernetesRepoModel,
    InternalKubernetesRepoModel,
)
from cilib.models.repos.snaps import (
    SnapKubeApiServerRepoModel,
    SnapKubeControllerManagerRepoModel,
    SnapKubeProxyRepoModel,
    SnapKubeSchedulerRepoModel,
    SnapKubectlRepoModel,
    SnapKubeadmRepoModel,
    SnapKubeletRepoModel,
    SnapKubernetesTestRepoModel,
    SnapCdkAddonsRepoModel,
)
from cilib.models.repos.debs import (
    DebCriToolsRepoModel,
    DebKubeadmRepoModel,
    DebKubectlRepoModel,
    DebKubeletRepoModel,
    DebKubernetesCniRepoModel,
)
from cilib.service.snap import SnapService
from cilib.service.deb import DebService
from drypy import dryrun


@click.group()
def cli():
    pass


@cli.command()
@click.option("--layer-list", required=True, help="Path to supported layer list")
@click.option("--charm-list", required=True, help="Path to supported charm list")
@click.option(
    "--ancillary-list",
    required=True,
    help="Path to additionall repos that need to be rebased.",
)
@click.option(
    "--filter-by-tag", required=False, help="only build for tags", multiple=True
)
@click.option("--dry-run", is_flag=True)
def cut_stable_release(layer_list, charm_list, ancillary_list, filter_by_tag, dry_run):
    return _cut_stable_release(
        layer_list, charm_list, ancillary_list, filter_by_tag, dry_run
    )


def _cut_stable_release(layer_list, charm_list, ancillary_list, filter_by_tag, dry_run):
    """This will merge each layers master onto the stable branches.

    PLEASE NOTE: This step should come after each stable branch has been tagged
    and references a current stable bundle revision.

    layer_list: YAML spec containing git repos and their upstream/downstream properties
    charm_list: YAML spec containing git repos and their upstream/downstream properties
    """
    layer_list = yaml.safe_load(Path(layer_list).read_text(encoding="utf8"))
    charm_list = yaml.safe_load(Path(charm_list).read_text(encoding="utf8"))
    ancillary_list = yaml.safe_load(Path(ancillary_list).read_text(encoding="utf8"))
    new_env = os.environ.copy()
    for layer_map in layer_list + charm_list + ancillary_list:
        for layer_name, repos in layer_map.items():
            downstream = repos["downstream"]
            if not repos.get("needs_stable", True):
                continue

            tags = repos.get("tags", None)
            if tags:
                if not any(match in filter_by_tag for match in tags):
                    continue

            log.info(f"Releasing :: {layer_name:^35} :: from: master to: stable")
            if not dry_run:
                downstream = f"https://{new_env['CDKBOT_GH_USR']}:{new_env['CDKBOT_GH_PSW']}@github.com/{downstream}"
                identifier = str(uuid.uuid4())
                os.makedirs(identifier)
                for line in git.clone(downstream, identifier, _iter=True):
                    log.info(line)
                git_rev_master = git(
                    "rev-parse", "origin/master", _cwd=identifier
                ).stdout.decode()
                git_rev_stable = git(
                    "rev-parse", "origin/stable", _cwd=identifier
                ).stdout.decode()
                if git_rev_master == git_rev_stable:
                    log.info(f"Skipping  :: {layer_name:^35} :: master == stable")
                    continue
                git.config("user.email", "cdkbot@juju.solutions", _cwd=identifier)
                git.config("user.name", "cdkbot", _cwd=identifier)
                git.config("--global", "push.default", "simple")
                git.checkout("-f", "stable", _cwd=identifier)
                git.merge("master", "--no-ff", _cwd=identifier)
                for line in git.push("origin", "stable", _cwd=identifier, _iter=True):
                    log.info(line)


def _tag_stable_forks(
    layer_list, charm_list, k8s_version, bundle_rev, filter_by_tag, bugfix, dry_run
):
    """Tags stable forks to a certain bundle revision for a k8s version

    layer_list: YAML spec containing git repos and their upstream/downstream properties
    bundle_rev: bundle revision to tag for a particular version of k8s

    git tag (ie. ck-{bundle_rev}), this would mean we tagged current
    stable branches for 1.14 with the latest charmed kubernetes(ck) bundle rev
    of {bundle_rev}

    TODO: Switch to different merge strategy
    git checkout master
    git checkout -b staging
    git merge stable -s ours
    git checkout stable
    git reset staging
    """
    layer_list = yaml.safe_load(Path(layer_list).read_text(encoding="utf8"))
    charm_list = yaml.safe_load(Path(charm_list).read_text(encoding="utf8"))
    new_env = os.environ.copy()
    for layer_map in layer_list + charm_list:
        for layer_name, repos in layer_map.items():

            tags = repos.get("tags", None)
            if tags:
                if not any(match in filter_by_tag for match in tags):
                    continue

            downstream = repos["downstream"]
            if bugfix:
                tag = f"{k8s_version}+{bundle_rev}"
            else:
                tag = f"ck-{k8s_version}-{bundle_rev}"
            if not repos.get("needs_tagging", True):
                log.info(f"Skipping {layer_name} :: does not require tagging")
                continue

            log.info(f"Tagging {layer_name} ({tag}) :: {repos['downstream']}")
            if not dry_run:
                downstream = f"https://{new_env['CDKBOT_GH_USR']}:{new_env['CDKBOT_GH_PSW']}@github.com/{downstream}"
                identifier = str(uuid.uuid4())
                os.makedirs(identifier)
                for line in git.clone(downstream, identifier, _iter=True):
                    log.info(line)
                git.config("user.email", "cdkbot@juju.solutions", _cwd=identifier)
                git.config("user.name", "cdkbot", _cwd=identifier)
                git.config("--global", "push.default", "simple")
                git.checkout("stable", _cwd=identifier)
                try:
                    for line in git.tag(
                        "--force", tag, _cwd=identifier, _iter=True, _bg_exc=False
                    ):
                        log.info(line)
                    for line in git.push(
                        "--force",
                        "origin",
                        tag,
                        _cwd=identifier,
                        _bg_exc=False,
                        _iter=True,
                    ):
                        log.info(line)
                except sh.ErrorReturnCode as error:
                    log.info(
                        f"Problem tagging: {error.stderr.decode().strip()}, will skip for now.."
                    )


@cli.command()
@click.option("--layer-list", required=True, help="Path to supported layer list")
@click.option("--charm-list", required=True, help="Path to supported charm list")
@click.option(
    "--k8s-version", required=True, help="Version of k8s this bundle provides"
)
@click.option(
    "--bundle-revision", required=True, help="Bundle revision to tag stable against"
)
@click.option(
    "--filter-by-tag", required=False, help="only build for tags", multiple=True
)
@click.option("--bugfix", is_flag=True)
@click.option("--dry-run", is_flag=True)
def tag_stable(
    layer_list, charm_list, k8s_version, bundle_revision, filter_by_tag, bugfix, dry_run
):
    return _tag_stable_forks(
        layer_list,
        charm_list,
        k8s_version,
        bundle_revision,
        filter_by_tag,
        bugfix,
        dry_run,
    )


def __run_git(args):
    username, password, layer_name, upstream, downstream = args
    log.info(f"Syncing {layer_name} :: {upstream} -> {downstream}")
    downstream = f"https://{username}:{password}@github.com/{downstream}"
    identifier = str(uuid.uuid4())
    os.makedirs(identifier)
    ret = capture(f"git clone {downstream} {identifier}")
    if not ret.ok:
        log.info(f"Failed to clone repo: {ret.stderr.decode()}")
        sys.exit(1)
    cmd_ok("git config user.email 'cdkbot@juju.solutions'", cwd=identifier)
    cmd_ok("git config user.name cdkbot", cwd=identifier)
    cmd_ok("git config push.default simple", cwd=identifier)
    cmd_ok(f"git remote add upstream {upstream}", cwd=identifier)
    cmd_ok("git fetch upstream", cwd=identifier)
    cmd_ok("git checkout master", cwd=identifier)
    cmd_ok("git merge upstream/master", cwd=identifier)
    cmd_ok("git push origin", cwd=identifier)
    cmd_ok("rm -rf {identifier}")


@cli.command()
@click.option("--dry-run", is_flag=True)
def debs(dry_run):
    """Syncs debs"""
    dryrun(dry_run)

    debs_to_process = [
        DebCriToolsRepoModel(),
        DebKubeadmRepoModel(),
        DebKubectlRepoModel(),
        DebKubeletRepoModel(),
        DebKubernetesCniRepoModel(),
    ]
    kubernetes_repo = InternalKubernetesRepoModel()

    # Sync all deb branches
    for _deb in debs_to_process:
        deb_service_obj = DebService(_deb, kubernetes_repo)
        deb_service_obj.sync_from_upstream()


@cli.command()
@click.option("--dry-run", is_flag=True)
def sync_internal_tags(dry_run):
    """Syncs upstream to downstream internal k8s tags"""
    dryrun(dry_run)
    # List of tuples containing upstream, downstream models and a starting semver
    repos_map = [
        (
            UpstreamKubernetesRepoModel(),
            InternalKubernetesRepoModel(),
            enums.K8S_STARTING_SEMVER,
        )
    ]

    for repo in repos_map:
        upstream, downstream, starting_semver = repo
        tags_to_sync = upstream.tags_subset_semver_point(downstream, starting_semver)
        if not tags_to_sync:
            click.echo(f"All synced up: {upstream} == {downstream}")
            continue
        upstream.clone()
        upstream.remote_add("downstream", downstream.repo, cwd=upstream.name)
        for tag in tags_to_sync:
            click.echo(f"Syncing repo {upstream} => {downstream}, tag => {tag}")
            upstream.push("downstream", tag, cwd=upstream.name)


@cli.command()
@click.option("--dry-run", is_flag=True)
def forks(dry_run):
    """Syncs all upstream forks"""
    # Try auto-merge; if conflict: update_readme.py && git add README.md && git
    # commit. If that fails, too, then it was a JSON conflict that will have to
    # be handled manually.

    layer_list = enums.CHARM_LAYERS_MAP
    charm_list = enums.CHARM_MAP
    new_env = os.environ.copy()
    username = quote(new_env["CDKBOT_GH_USR"])
    password = quote(new_env["CDKBOT_GH_PSW"])

    repos_to_process = []
    for layer_map in layer_list + charm_list:
        for layer_name, repos in layer_map.items():
            upstream = repos["upstream"]
            downstream = repos["downstream"]
            if urlparse(upstream).path.lstrip("/") == downstream:
                log.info(f"Skipping {layer_name} :: {upstream} == {downstream}")
                continue
            items = (username, password, layer_name, upstream, downstream)
            log.info(f"Adding {layer_name} to queue")
            repos_to_process.append(items)

    if not dry_run:
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as tp:
            git_runs = {tp.submit(__run_git, args): args for args in repos_to_process}
            for future in concurrent.futures.as_completed(git_runs):
                try:
                    data = future.result()
                except Exception as exc:
                    log.info(f"Failed thread: {exc}")


@cli.command()
@click.option("--dry-run", is_flag=True)
def snaps(dry_run):
    """Syncs the snap branches, keeps snap builds in sync, and makes sure the latest snaps are published into snap store"""
    dryrun(dry_run)
    snaps_to_process = [
        SnapKubeApiServerRepoModel(),
        SnapKubeControllerManagerRepoModel(),
        SnapKubeProxyRepoModel(),
        SnapKubeSchedulerRepoModel(),
        SnapKubectlRepoModel(),
        SnapKubeadmRepoModel(),
        SnapKubeletRepoModel(),
        SnapKubernetesTestRepoModel(),
    ]

    kubernetes_repo = UpstreamKubernetesRepoModel()

    # Sync all snap branches
    for _snap in snaps_to_process:
        snap_service_obj = SnapService(_snap, kubernetes_repo)
        snap_service_obj.sync_from_upstream()
        snap_service_obj.sync_all_track_snaps()
        snap_service_obj.sync_stable_track_snaps()

    # Handle cdk-addons sync separetely
    cdk_addons = SnapCdkAddonsRepoModel()
    cdk_addons_service_obj = SnapService(cdk_addons, kubernetes_repo)
    cdk_addons_service_obj.sync_stable_track_snaps()


if __name__ == "__main__":
    cli()
