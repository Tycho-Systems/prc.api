import setuptools


setuptools.setup(
    name="prc.api",
    version="0.2.2",
    author="tycho",
    url="https://github.com/Tycho-Systems/prc.api",
    license="MIT",
    description="prc.api is an asynchronous Python wrapper for the PRC API.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    packages=setuptools.find_packages(),
    include_package_data=True,
    python_requires=">=3.8",
    install_requires=["httpx", "asyncio"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Framework :: AsyncIO",
    ],
)
