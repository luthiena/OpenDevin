"""Test the EventStreamRuntime, which connects to the RuntimeClient running in the sandbox."""

import asyncio
import os
import pathlib
import tempfile
import time
from unittest.mock import patch

import pytest

from opendevin.core.config import AppConfig, SandboxConfig
from opendevin.core.logger import opendevin_logger as logger
from opendevin.events import EventStream
from opendevin.events.action import (
    BrowseURLAction,
    CmdRunAction,
    FileReadAction,
    FileWriteAction,
    IPythonRunCellAction,
)
from opendevin.events.observation import (
    BrowserOutputObservation,
    CmdOutputObservation,
    ErrorObservation,
    FileReadObservation,
    FileWriteObservation,
    IPythonRunCellObservation,
)
from opendevin.runtime.client.runtime import EventStreamRuntime
from opendevin.runtime.plugins import AgentSkillsRequirement, JupyterRequirement
from opendevin.runtime.server.runtime import ServerRuntime
from opendevin.storage import get_file_store


@pytest.fixture(autouse=True)
def print_method_name(request):
    print('\n########################################################################')
    print(f'Running test: {request.node.name}')
    print('########################################################################')


@pytest.fixture
def temp_dir(monkeypatch):
    # get a temporary directory
    with tempfile.TemporaryDirectory() as temp_dir:
        pathlib.Path().mkdir(parents=True, exist_ok=True)
        yield temp_dir


# This assures that all tests run together for each runtime, not alternating between them,
# which caused them to fail previously.
@pytest.fixture(scope='module', params=[EventStreamRuntime, ServerRuntime])
def box_class(request):
    time.sleep(1)
    return request.param


async def _load_runtime(temp_dir, box_class):
    sid = 'test'
    cli_session = 'main_test'
    plugins = [JupyterRequirement(), AgentSkillsRequirement()]
    config = AppConfig(
        workspace_base=temp_dir,
        workspace_mount_path=temp_dir,
        sandbox=SandboxConfig(
            use_host_network=True,
        ),
    )
    file_store = get_file_store(config.file_store, config.file_store_path)
    event_stream = EventStream(cli_session, file_store)

    container_image = config.sandbox.container_image
    # NOTE: we will use the default container image specified in the config.sandbox
    # if it is an official od_runtime image.
    if 'od_runtime' not in container_image:
        container_image = 'ubuntu:22.04'
        logger.warning(
            f'`{config.sandbox.container_image}` is not an od_runtime image. Will use `{container_image}` as the container image for testing.'
        )
    if box_class == EventStreamRuntime:
        runtime = EventStreamRuntime(
            config=config,
            event_stream=event_stream,
            sid=sid,
            # NOTE: we probably don't have a default container image `/sandbox` for the event stream runtime
            # Instead, we will pre-build a suite of container images with OD-runtime-cli installed.
            container_image=container_image,
            plugins=plugins,
        )
        await runtime.ainit()
    elif box_class == ServerRuntime:
        runtime = ServerRuntime(config=config, event_stream=event_stream, sid=sid)
        await runtime.ainit()
        runtime.init_sandbox_plugins(plugins)
        runtime.init_runtime_tools(
            [],
            is_async=False,
            runtime_tools_config={},
        )
    else:
        raise ValueError(f'Invalid box class: {box_class}')
    await asyncio.sleep(1)
    return runtime


@pytest.mark.asyncio
async def test_env_vars_os_environ(temp_dir, box_class):
    with patch.dict(os.environ, {'SANDBOX_ENV_FOOBAR': 'BAZ'}):
        runtime = await _load_runtime(temp_dir, box_class)

        obs: CmdOutputObservation = await runtime.run_action(
            CmdRunAction(command='env')
        )
        print(obs)

        obs: CmdOutputObservation = await runtime.run_action(
            CmdRunAction(command='echo $FOOBAR')
        )
        print(obs)
        assert obs.exit_code == 0, 'The exit code should be 0.'
        assert (
            obs.content.strip().split('\n\r')[0].strip() == 'BAZ'
        ), f'Output: [{obs.content}] for {box_class}'

        await runtime.close()
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_env_vars(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)
    await runtime.add_env_vars({'QUUX': 'abc"def'})

    obs: CmdOutputObservation = await runtime.run_action(
        CmdRunAction(command='echo $QUUX')
    )
    print(obs)
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert (
        obs.content.strip().split('\r\n')[0].strip() == 'abc"def'
    ), f'Output: [{obs.content}] for {box_class}'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_empty_dict(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    prev_obs = await runtime.run_action(CmdRunAction(command='env'))
    assert prev_obs.exit_code == 0, 'The exit code should be 0.'
    print(prev_obs)

    await runtime.add_env_vars({})

    obs = await runtime.run_action(CmdRunAction(command='env'))
    assert obs.exit_code == 0, 'The exit code should be 0.'
    print(obs)
    assert (
        obs.content == prev_obs.content
    ), 'The env var content should be the same after adding an empty dict.'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_multiple_env_vars(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)
    await runtime.add_env_vars({'QUUX': 'abc"def', 'FOOBAR': 'xyz'})

    obs: CmdOutputObservation = await runtime.run_action(
        CmdRunAction(command='echo $QUUX $FOOBAR')
    )
    print(obs)
    assert obs.exit_code == 0, 'The exit code should be 0.'
    assert (
        obs.content.strip().split('\r\n')[0].strip() == 'abc"def xyz'
    ), f'Output: [{obs.content}] for {box_class}'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_env_vars_runtime_add_env_vars_overwrite(temp_dir, box_class):
    with patch.dict(os.environ, {'SANDBOX_ENV_FOOBAR': 'BAZ'}):
        runtime = await _load_runtime(temp_dir, box_class)
        await runtime.add_env_vars({'FOOBAR': 'xyz'})

        obs: CmdOutputObservation = await runtime.run_action(
            CmdRunAction(command='echo $FOOBAR')
        )
        print(obs)
        assert obs.exit_code == 0, 'The exit code should be 0.'
        assert (
            obs.content.strip().split('\r\n')[0].strip() == 'xyz'
        ), f'Output: [{obs.content}] for {box_class}'

        await runtime.close()
        await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_bash_command_pexcept(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    # We set env var PS1="\u@\h:\w $"
    # and construct the PEXCEPT prompt base on it.
    # When run `env`, bad implementation of CmdRunAction will be pexcepted by this
    # and failed to pexcept the right content, causing it fail to get error code.
    obs = await runtime.run_action(CmdRunAction(command='env'))

    # For example:
    # 02:16:13 - opendevin:DEBUG: client.py:78 - Executing command: env
    # 02:16:13 - opendevin:DEBUG: client.py:82 - Command output: PYTHONUNBUFFERED=1
    # CONDA_EXE=/opendevin/miniforge3/bin/conda
    # [...]
    # LC_CTYPE=C.UTF-8
    # PS1=\u@\h:\w $
    # 02:16:13 - opendevin:DEBUG: client.py:89 - Executing command for exit code: env
    # 02:16:13 - opendevin:DEBUG: client.py:92 - Exit code Output:
    # CONDA_DEFAULT_ENV=base

    # As long as the exit code is 0, the test will pass.
    assert isinstance(
        obs, CmdOutputObservation
    ), 'The observation should be a CmdOutputObservation.'
    assert obs.exit_code == 0, 'The exit code should be 0.'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_simple_cmd_ipython_and_fileop(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    # Test run command
    action_cmd = CmdRunAction(command='ls -l')
    logger.info(action_cmd, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_cmd)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    assert 'total 0' in obs.content

    # Test run ipython
    test_code = "print('Hello, `World`!\\n')"
    action_ipython = IPythonRunCellAction(code=test_code)
    logger.info(action_ipython, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_ipython)
    assert isinstance(obs, IPythonRunCellObservation)

    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert obs.content.strip() == 'Hello, `World`!'

    # Test read file (file should not exist)
    action_read = FileReadAction(path='hello.sh')
    logger.info(action_read, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_read)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})
    assert isinstance(obs, ErrorObservation)
    assert 'File not found' in obs.content

    # Test write file
    action_write = FileWriteAction(content='echo "Hello, World!"', path='hello.sh')
    logger.info(action_write, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_write)
    assert isinstance(obs, FileWriteObservation)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert obs.content == ''
    if box_class == ServerRuntime:
        assert obs.path == 'hello.sh'
    else:
        # event stream runtime will always use absolute path
        assert obs.path == '/workspace/hello.sh'

    # Test read file (file should exist)
    action_read = FileReadAction(path='hello.sh')
    logger.info(action_read, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_read)
    assert isinstance(
        obs, FileReadObservation
    ), 'The observation should be a FileReadObservation.'
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert obs.content == 'echo "Hello, World!"\n'
    if box_class == ServerRuntime:
        assert obs.path == 'hello.sh'
    else:
        assert obs.path == '/workspace/hello.sh'

    await runtime.close()
    await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_simple_browse(temp_dir, box_class):
    runtime = await _load_runtime(temp_dir, box_class)

    # Test browse
    action_cmd = CmdRunAction(command='python -m http.server 8000 > server.log 2>&1 &')
    logger.info(action_cmd, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_cmd)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, CmdOutputObservation)
    assert obs.exit_code == 0
    assert '[1]' in obs.content

    action_browse = BrowseURLAction(url='http://localhost:8000')
    logger.info(action_browse, extra={'msg_type': 'ACTION'})
    obs = await runtime.run_action(action_browse)
    logger.info(obs, extra={'msg_type': 'OBSERVATION'})

    assert isinstance(obs, BrowserOutputObservation)
    assert obs.url == 'http://localhost:8000'
    assert obs.status_code == 200
    assert not obs.error
    assert obs.open_pages_urls == ['http://localhost:8000/']
    assert obs.active_page_index == 0
    assert obs.last_browser_action == 'goto("http://localhost:8000")'
    assert obs.last_browser_action_error == ''
    assert 'Directory listing for /' in obs.content
    assert 'server.log' in obs.content

    await runtime.close()
