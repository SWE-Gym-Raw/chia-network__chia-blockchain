from __future__ import annotations

import collections
import json
import os
import re
import shlex
import urllib
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Collection,
    Literal,
    Optional,
    Sequence,
    TypeVar,
    Union,
    overload,
)

import anyio
import anyio.streams.memory
import click
import yaml

from chia.cmds.cmd_classes import chia_command, option


class UnexpectedFormError(Exception):
    pass


Oses = Union[Literal["linux"], Literal["macos-arm"], Literal["macos-intel"], Literal["windows"]]
Method = Union[Literal["GET"], Literal["POST"]]
Per = Union[Literal["directory"], Literal["file"]]

all_oses: Sequence[Oses] = ("linux", "macos-arm", "macos-intel", "windows")


def report(*args: str) -> None:
    print("    ====", *args)


@overload
async def run_gh_api(method: Method, args: list[str], error: str) -> None: ...
@overload
async def run_gh_api(method: Method, args: list[str], error: str, capture_stdout: Literal[False]) -> None: ...
@overload
async def run_gh_api(method: Method, args: list[str], error: str, capture_stdout: Literal[True]) -> str: ...


async def run_gh_api(method: Method, args: list[str], error: str, capture_stdout: bool = False) -> Optional[str]:
    command = [
        "gh",
        "api",
        # "--paginate",
        f"--method={method}",
        "-H=Accept: application/vnd.github+json",
        "-H=X-GitHub-Api-Version: 2022-11-28",
        *args,
    ]
    report(f"running command: {shlex.join(command)}")

    if capture_stdout:
        process = await anyio.run_process(command=command, check=False, stderr=None)
    else:
        process = await anyio.run_process(command=command, check=False, stderr=None, stdout=None)

    if process.returncode != 0:
        raise click.ClickException(error)

    if capture_stdout:
        return process.stdout.decode("utf-8")

    return None


@click.group("gh", help="For working with GitHub")
def gh_group() -> None:
    pass


@chia_command(
    gh_group,
    name="test",
    short_help="launch a test run in CI from HEAD or existing remote ref",
    help="""Allows easy triggering and viewing of test workflow runs in CI including
    configuration of parameters.  If a ref is specified then it must exist on the
    remote and a run will be launched for it.  If ref is not specified then the local
    HEAD will be pushed to a temporary remote branch and a run will be launched for
    that.  There is no need to push the local commit first.  The temporary remote
    branch will automatically be deleted in most cases.

    After launching the workflow run GitHub will be queried for the run and the URL
    will be opened in the default browser.
    """,
)
class TestCMD:
    workflow_id: ClassVar[str] = "test.yml"
    owner: str = option("-o", "--owner", help="Owner of the repo", type=str, default="Chia-Network")
    repository: str = option("-r", "--repository", help="Repository name", type=str, default="chia-blockchain")
    ref: Optional[str] = option(
        "-f",
        "--ref",
        help="Branch or tag name (commit SHA not supported), if not specified will push HEAD to a temporary branch",
        type=str,
        default=None,
    )
    per: Per = option("-p", "--per", help="Per", type=click.Choice(["directory", "file"]), default="directory")
    only: Optional[Path] = option(
        "-o", "--only", help="Only run this item, a file or directory depending on --per", type=Path
    )
    duplicates: int = option("-d", "--duplicates", help="Number of duplicates", type=int, default=1)
    oses: Sequence[Oses] = option(
        "--os",
        help="Operating systems to run on",
        type=click.Choice(all_oses),
        multiple=True,
        default=all_oses,
    )
    full_python_matrix: bool = option(
        "--full-python-matrix/--default-python-matrix", help="Run on all Python versions", default=False
    )
    remote: str = option("-r", "--remote", help="Name of git remote", type=str, default="origin")

    async def run(self) -> None:
        await self.check_only()

        username = await self.get_username()

        if self.ref is not None:
            await self.trigger_workflow(self.ref)
            query = "+".join(
                [
                    "event=workflow_dispatch",
                    f"branch={self.ref}",
                    f"actor={username}",
                ]
            )
            run_url = f"https://github.com/Chia-Network/chia-blockchain/actions/workflows/test.yml?query={urllib.parse.quote(query)}"
            report(f"waiting a few seconds to load: {run_url}")
            await anyio.sleep(10)
        else:
            process = await anyio.run_process(command=["git", "rev-parse", "HEAD"], check=True, stderr=None)
            if process.returncode != 0:
                raise click.ClickException("Failed to get current commit SHA")

            commit_sha = process.stdout.decode("utf-8").strip()

            temp_branch_name = f"tmp/{username}/{commit_sha}/{uuid.uuid4()}"

            process = await anyio.run_process(
                command=["git", "push", self.remote, f"HEAD:{temp_branch_name}"], check=False, stdout=None, stderr=None
            )
            if process.returncode != 0:
                raise click.ClickException("Failed to push temporary branch")

            try:
                await self.trigger_workflow(temp_branch_name)
                for _ in range(10):
                    await anyio.sleep(1)

                    try:
                        report("looking for run")
                        run_url = await self.find_run(temp_branch_name)
                        report(f"run found at: {run_url}")
                    except UnexpectedFormError:
                        report("run not found")
                        continue

                    break
                else:
                    raise click.ClickException("Failed to find run url")
            finally:
                report(f"deleting temporary branch: {temp_branch_name}")
                process = await anyio.run_process(
                    command=["git", "push", self.remote, "-d", temp_branch_name], check=False, stdout=None, stderr=None
                )
                if process.returncode != 0:
                    raise click.ClickException("Failed to dispatch workflow")
                report(f"temporary branch deleted: {temp_branch_name}")

        report(f"run url: {run_url}")
        webbrowser.open(run_url)

    async def check_only(self) -> None:
        if self.only is not None:
            import chia._tests

            test_path = Path(chia._tests.__file__).parent
            effective_path = test_path.joinpath(self.only)
            checks: dict[Per, Callable[[], bool]] = {"directory": effective_path.is_dir, "file": effective_path.is_file}
            check = checks[self.per]
            if not check():
                if effective_path.exists():
                    explanation = "wrong type"
                else:
                    explanation = "does not exist"
                message = f"expected requested --only to be a {self.per}, {explanation} at: {effective_path.as_posix()}"
                raise click.ClickException(message)

    async def trigger_workflow(self, ref: str) -> None:
        def input_arg(name: str, value: object, cond: bool = True) -> list[str]:
            if not cond:
                return []

            assert value is not None

            if isinstance(value, os.PathLike):
                value = os.fspath(value)
            dumped = yaml.safe_dump(value).partition("\n")[0]
            return [f"-f=inputs[{name}]={dumped}"]

        # https://docs.github.com/en/rest/actions/workflows?apiVersion=2022-11-28#create-a-workflow-dispatch-event
        await run_gh_api(
            method="POST",
            args=[
                f"/repos/{self.owner}/{self.repository}/actions/workflows/{self.workflow_id}/dispatches",
                f"-f=ref={ref}",
                *input_arg("per", self.per),
                *input_arg("only", self.only, self.only is not None),
                *input_arg("duplicates", self.duplicates),
                *(arg for os_name in all_oses for arg in input_arg(f"run-{os_name}", os_name in self.oses)),
                *input_arg("full-python-matrix", self.full_python_matrix),
            ],
            error="Failed to dispatch workflow",
        )
        report(f"workflow triggered on branch: {ref}")

    async def find_run(self, ref: str) -> str:
        # https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28#list-workflow-runs-for-a-workflow
        stdout = await run_gh_api(
            method="GET",
            args=[
                f"-f=branch={ref}",
                f"/repos/{self.owner}/{self.repository}/actions/workflows/{self.workflow_id}/runs",
            ],
            error="Failed to query workflow runs",
            capture_stdout=True,
        )

        response = json.loads(stdout)
        runs = response["workflow_runs"]
        try:
            [run] = runs
        except ValueError:
            raise UnexpectedFormError(f"expected 1 run, got: {len(runs)}")

        url = run["html_url"]

        assert isinstance(url, str), f"expected url to be a string, got: {url!r}"
        return url

    async def get_username(self) -> str:
        # https://docs.github.com/en/rest/users/users?apiVersion=2022-11-28#get-the-authenticated-user
        stdout = await run_gh_api(
            method="GET",
            args=["/user"],
            error="Failed to get username",
            capture_stdout=True,
        )

        response = json.loads(stdout)
        username = response["login"]
        assert isinstance(username, str), f"expected username to be a string, got: {username!r}"
        return username


T = TypeVar("T")
U = TypeVar("U")


async def worker(
    handler: Callable[[T], Awaitable[U]],
    arg_stream: anyio.streams.memory.MemoryObjectReceiveStream[T],
    result_stream: anyio.streams.memory.MemoryObjectSendStream[U],
) -> None:
    async with arg_stream, result_stream:
        async for arg in arg_stream:
            try:
                result = await handler(arg)
            except Exception as e:
                report(f"worker failed on {arg!r}: {e}")
                continue
            await result_stream.send(result)


async def collect(
    args: Collection[T], handler: Callable[[T], Awaitable[U]], capacity: int, max_stagger: float = 1
) -> AsyncIterator[U]:
    arg_send_stream, arg_receive_stream = anyio.create_memory_object_stream[T](max_buffer_size=len(args))
    result_send_stream, result_receive_stream = anyio.create_memory_object_stream[U]()

    needed_capacity = min(capacity, len(args))
    single_batch = needed_capacity >= len(args)

    async with anyio.create_task_group() as task_group:
        for i in range(needed_capacity):
            if not single_batch and i != 0:
                await anyio.sleep(max_stagger / needed_capacity)
            task_group.start_soon(worker, handler, arg_receive_stream.clone(), result_send_stream.clone())

        # TODO: this seems awkward
        arg_receive_stream.close()
        result_send_stream.close()

        async with arg_send_stream:
            for arg in args:
                await arg_send_stream.send(arg)

        async with result_receive_stream:
            async for result in result_receive_stream:
                yield result

        task_group.cancel_scope.cancel()


@dataclass
class RunInfo:
    workflow_id: int
    run_number: int
    id: int
    status: str
    conclusion: str
    name: str
    attempts: int
    url: str


@chia_command(
    gh_group,
    name="rerun",
    # TODO: helpy helper
    short_help="",
    help="""""",
)
class RerunCMD:
    owner: str = option("-o", "--owner", help="Owner of the repo", type=str, default="Chia-Network")
    repository: str = option("-r", "--repository", help="Repository name", type=str, default="chia-blockchain")
    pr: int = option("--pr", help="Pull request number", type=int, required=True)
    max_attempts: int = option("--max-attempts", help="Maximum number of attempts", type=int, default=3)

    async def run(self) -> None:
        head_sha = await self.get_pull_request_head_sha()
        run_ids = await self.get_run_ids_for_sha(sha=head_sha)
        print(len(run_ids), run_ids)

        all_runs = collections.defaultdict(list)
        async for run in collect(args=run_ids, handler=self.get_run, capacity=10):
            all_runs[run.workflow_id].append(run)

        runs = [max(runs, key=lambda run: run.run_number) for runs in all_runs.values()]
        to_rerun: list[int] = []
        for run in runs:
            if run.conclusion in {"failure", "cancelled"}:
                if run.attempts < self.max_attempts:
                    # print("    ++++ would retrigger", run)
                    to_rerun.append(run.id)
                else:
                    print("    ---- giving up on", run.url)

        async for _ in collect(args=to_rerun, handler=self.rerun_job, capacity=10):
            pass

    async def get_run(self, run_id: int) -> RunInfo:
        # https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28#get-a-workflow-run
        stdout = await run_gh_api(
            method="GET",
            args=[
                f"/repos/{self.owner}/{self.repository}/actions/runs/{run_id}",
            ],
            error="Failed to get run",
            capture_stdout=True,
        )
        response = json.loads(stdout)

        return RunInfo(
            id=response["id"],
            name=response["name"],
            status=response["status"],
            conclusion=response["conclusion"],
            workflow_id=response["workflow_id"],
            run_number=response["run_number"],
            attempts=response["run_attempt"],
            url=response["html_url"],
        )

    async def get_check_runs(self, suite_id: int) -> dict[str, Any]:
        # https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28#list-check-runs-in-a-check-suite
        stdout = await run_gh_api(
            method="GET",
            args=[
                f"/repos/{self.owner}/{self.repository}/check-suites/{suite_id}/check-runs",
            ],
            error="Failed to get check runs",
            capture_stdout=True,
        )
        result = json.loads(stdout)
        assert isinstance(result, dict)
        return result

    # TODO: but is this only the latest? mm...
    async def get_run_ids_for_sha(self, sha: str) -> list[int]:
        # https://docs.github.com/en/rest/checks/suites?apiVersion=2022-11-28#list-check-suites-for-a-git-reference
        stdout = await run_gh_api(
            method="GET",
            args=[
                "--paginate",
                # "-f=per_page=100",
                f"/repos/{self.owner}/{self.repository}/commits/{sha}/check-suites",
            ],
            error="Failed to get check suites",
            capture_stdout=True,
        )
        response = json.loads(stdout)
        check_suite_ids = {
            check_suite["id"]
            for check_suite in response["check_suites"]
            if check_suite["app"]["slug"] == "github-actions"
        }
        print(response["total_count"], len(check_suite_ids), check_suite_ids)
        # assert len(check_suite_ids) == response["total_count"]

        run_ids = []
        # for suite_id in check_suite_ids:
        async for response in collect(args=check_suite_ids, handler=self.get_check_runs, capacity=10):
            for check_run in response["check_runs"]:
                if check_run["app"]["slug"] != "github-actions":
                    continue

                match = re.match(r"^.*/runs/(.*)/job/.*$", check_run["html_url"])
                assert match is not None
                value = int(match[1])
                run_ids.append(value)
                break
            else:
                report(f"no run found for suite: {response['id']}")

        return sorted(run_ids)

    # # TODO: but is this only the latest? mm...
    # async def get_run_ids_for_sha(self, sha: str) -> set[int]:
    #     # https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28#list-check-runs-for-a-git-reference
    #     stdout = await run_gh_api(
    #         method="GET",
    #         args=[
    #             f"/repos/{self.owner}/{self.repository}/commits/{sha}/check-runs",
    #         ],
    #         error="Failed to get check runs",
    #         capture_stdout=True,
    #     )
    #     response = json.loads(stdout)
    #     run_ids = {
    #         # https://github.com/Chia-Network/chia-blockchain/actions/runs/11977927817/job/33400686549
    #         int(re.match(r"^.*/runs/(.*)/job/.*$", check_run["html_url"])[1])
    #         for check_run in response["check_runs"]
    #         if check_run["app"]["slug"] == "github-actions"
    #     }
    #
    #     return run_ids
    #
    async def get_pull_request_head_sha(self) -> str:
        # https://docs.github.com/en/rest/pulls/pulls?apiVersion=2022-11-28#get-a-pull-request
        stdout = await run_gh_api(
            method="GET",
            args=[
                f"/repos/{self.owner}/{self.repository}/pulls/{self.pr}",
            ],
            error="Failed to get pull request",
            capture_stdout=True,
        )
        response = json.loads(stdout)
        result = response["head"]["sha"]
        assert isinstance(result, str)
        return result

    async def rerun_job(self, run_id: int) -> None:
        # https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28#re-run-failed-jobs-from-a-workflow-run
        await run_gh_api(
            method="POST",
            args=[
                f"/repos/{self.owner}/{self.repository}/actions/runs/{run_id}/rerun-failed-jobs",
            ],
            error="Failed to rerun failed jobs",
        )
