"""
Docker build helper: compiles deobf/bin/luau-ast (Luau.Ast.CLI) from the
same patched Luau checkout that deobf/build_luau.py uses for `luau`
(Luau.Repl.CLI). Not part of the deobfuscation engine itself — the engine
only needs this binary for its own checks (luau-ast.exe parse-error check
mentioned in CLAUDE.md); kept as a separate script instead of editing
deobf/build_luau.py so that file stays exactly as provided.

    python docker/build_luau_ast.py [--tag 0.739] [--portable]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

TAG = "0.739"
REPO = "https://github.com/luau-lang/luau.git"
FREEZE = "    lua_setreadonly(L, -1, true);\n    lua_pop(L, 1); // pop the metatable\n"
PATCHED = "    // deobf: left writable, envlog.luau adds Roblox's Vector3 members\n    lua_pop(L, 1); // pop the metatable\n"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(HERE, "deobf", "bin")


def run(cmd, cwd=None):
    print("[*] " + " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, cwd=cwd, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--portable", action="store_true", help="no -march=native")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="luau-ast-build-")
    src = os.path.join(tmp, "luau")
    run(["git", "clone", "-q", "--depth", "1", "--branch", args.tag, REPO, src])

    path = os.path.join(src, "VM", "src", "lveclib.cpp")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.count(FREEZE) != 1:
        sys.exit("[!] lveclib.cpp changed upstream: patch createmetatable() by hand")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.replace(FREEZE, PATCHED))

    opt = "-O3 -flto" + ("" if args.portable else " -march=native")
    build = os.path.join(src, "build-deobf")
    run(["cmake", "-S", src, "-B", build, "-DCMAKE_BUILD_TYPE=Release",
         "-DLUAU_BUILD_TESTS=OFF", "-DLUAU_STATIC_CRT=ON", "-G", "Ninja",
         "-DCMAKE_C_COMPILER=gcc", "-DCMAKE_CXX_COMPILER=g++",
         "-DCMAKE_C_FLAGS_RELEASE=%s -DNDEBUG" % opt,
         "-DCMAKE_CXX_FLAGS_RELEASE=%s -DNDEBUG" % opt,
         "-DCMAKE_EXE_LINKER_FLAGS=-static " + opt])
    run(["cmake", "--build", build, "--config", "Release", "--target", "Luau.Ast.CLI", "--parallel"])

    os.makedirs(BIN, exist_ok=True)
    shutil.copy2(os.path.join(build, "luau-ast"), os.path.join(BIN, "luau-ast"))
    print("[+] wrote " + os.path.join(BIN, "luau-ast"), file=sys.stderr)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
