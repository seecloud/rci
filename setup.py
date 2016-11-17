from setuptools import setup, find_packages

setup(
    name="rci",
    version="0.1.1a1",
    packages=find_packages(),
    include_package_data=True,
    install_requires=["pyyaml", "aiohttp", "asyncssh"],
    entry_points={"console_scripts": ["rci = rci.daemon:run"]}
)
