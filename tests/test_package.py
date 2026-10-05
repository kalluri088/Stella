import stella


def test_stella_package_is_importable() -> None:
    assert stella.__version__ == "1.5.0"


def test_the_package_version_is_the_one_metadata_reports() -> None:
    # The whole point of hatch reading __version__ from this file: the
    # wheel name, /version, `stella --version` and the git tag are all
    # derived from one number. It used to be possible for pyproject.toml
    # to say 1.4.0 while this file still said 0.1.0; this assertion is
    # what makes that state un-shippable.
    from importlib.metadata import version

    assert stella.__version__ == version("stella")
