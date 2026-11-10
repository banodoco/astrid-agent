# CPython standard-library selector excerpt, PSF license.
# Source: https://raw.githubusercontent.com/python/cpython/v3.12.10/Lib/ensurepip/__init__.py
# Complete upstream source SHA256: 90f6c76cd9ef7ed6daa198bb9ef771139683487f1b061211be7aa511831b2028
import collections
import os
import sysconfig

_PACKAGE_NAMES = ('pip',)

_PIP_VERSION = "25.0.1"

_PROJECTS = [
    ("pip", _PIP_VERSION, "py3"),
]

_Package = collections.namedtuple('Package',
                                  ('version', 'wheel_name', 'wheel_path'))

_WHEEL_PKG_DIR = sysconfig.get_config_var('WHEEL_PKG_DIR')

def _find_packages(path):
    packages = {}
    try:
        filenames = os.listdir(path)
    except OSError:
        # Ignore: path doesn't exist or permission error
        filenames = ()
    # Make the code deterministic if a directory contains multiple wheel files
    # of the same package, but don't attempt to implement correct version
    # comparison since this case should not happen.
    filenames = sorted(filenames)
    for filename in filenames:
        # filename is like 'pip-21.2.4-py3-none-any.whl'
        if not filename.endswith(".whl"):
            continue
        for name in _PACKAGE_NAMES:
            prefix = name + '-'
            if filename.startswith(prefix):
                break
        else:
            continue

        # Extract '21.2.4' from 'pip-21.2.4-py3-none-any.whl'
        version = filename.removeprefix(prefix).partition('-')[0]
        wheel_path = os.path.join(path, filename)
        packages[name] = _Package(version, None, wheel_path)
    return packages

def _get_packages():
    global _PACKAGES, _WHEEL_PKG_DIR
    if _PACKAGES is not None:
        return _PACKAGES

    packages = {}
    for name, version, py_tag in _PROJECTS:
        wheel_name = f"{name}-{version}-{py_tag}-none-any.whl"
        packages[name] = _Package(version, wheel_name, None)
    if _WHEEL_PKG_DIR:
        dir_packages = _find_packages(_WHEEL_PKG_DIR)
        # only used the wheel package directory if all packages are found there
        if all(name in dir_packages for name in _PACKAGE_NAMES):
            packages = dir_packages
    _PACKAGES = packages
    return packages

_PACKAGES = None

def version():
    """
    Returns a string specifying the bundled version of pip.
    """
    return _get_packages()['pip'].version
