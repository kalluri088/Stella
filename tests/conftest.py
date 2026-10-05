"""One rule every test here shares: never touch the real user profile.

Stella resolves its state directory through ``platformdirs``, which reads
``$XDG_DATA_HOME`` on Linux and macOS and ``%LOCALAPPDATA%`` on Windows. So
the isolation every test file already performs — pointing the XDG variables at
a ``tmp_path`` — changes nothing on Windows, where ``user_data_dir`` ignores
them and answers with the caller's own profile. Those tests still pass there
while creating ``%LOCALAPPDATA%\\stella``: config.json, the databases and an
``api_keys.json`` full of other tests' keys, in the real user directory.

platformdirs' supported hook for that is ``WIN_PD_OVERRIDE_LOCAL_APPDATA``,
which data, config, cache and runtime all derive from. It is one knob for four
directories, so a test that deliberately separates ``XDG_DATA_HOME`` from
``XDG_CONFIG_HOME`` cannot isolate both and lands here instead. The value has
to be an absolute path with a drive, which is exactly what pytest's ``tmp_path``
already is.

The session directory below is the safety net, not the mechanism: a test that
forgets to isolate gets a scratch profile rather than the owner's. Tests that
do isolate mirror it themselves, to the same base they point XDG at, because
their assertions are about that base.
"""

import os

import pytest

from stella.portable import WINDOWS, platform_name

#: The one knob platformdirs honours on Windows for all four user directories.
WIN_APPDATA = "WIN_PD_OVERRIDE_LOCAL_APPDATA"

IS_WINDOWS = platform_name() == WINDOWS


@pytest.fixture(scope="session", autouse=True)
def never_the_real_profile(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep every Windows test out of the real ``%LOCALAPPDATA%``."""

    if not IS_WINDOWS or os.environ.get(WIN_APPDATA, "").strip():
        yield
        return
    profile = tmp_path_factory.mktemp("appdata")
    os.environ[WIN_APPDATA] = str(profile)
    try:
        yield
    finally:
        os.environ.pop(WIN_APPDATA, None)
