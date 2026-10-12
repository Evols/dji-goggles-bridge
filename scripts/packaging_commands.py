"""Setuptools source-manifest support for the scripts/ directory."""

from pathlib import Path

from setuptools.command.egg_info import egg_info, manifest_maker


class EggInfo(egg_info):
    """Use the relocated template when generating the sdist file list."""

    def find_sources(self) -> None:
        maker = manifest_maker(self.distribution)
        maker.template = "scripts/MANIFEST.in"
        maker.manifest = str(Path(self.egg_info) / "SOURCES.txt")
        maker.ignore_egg_info_dir = self.ignore_egg_info_in_manifest
        maker.run()
        self.filelist = maker.filelist
