"""Make pip-installed NVIDIA DLLs (cuBLAS, cuDNN) visible on Windows. Import before ctranslate2/onnxruntime."""
import os
import site
import sys


def setup():
    if sys.platform != "win32":
        return
    roots = site.getsitepackages() + [site.getusersitepackages()]
    for root in roots:
        nv = os.path.join(root, "nvidia")
        if not os.path.isdir(nv):
            continue
        for pkg in os.listdir(nv):
            d = os.path.join(nv, pkg, "bin")
            if os.path.isdir(d):
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]


setup()
