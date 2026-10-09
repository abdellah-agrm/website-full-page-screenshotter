"""
Screenshotter Pro - Executable Build Script
Compiles the application into a standalone Windows binary (ScreenshotterPro.exe) with custom app icon.
"""
import os
import sys
import subprocess
from pathlib import Path


def main():
    print("=== Screenshotter Pro Builder ===")
    project_dir = Path(__file__).parent.resolve()
    os.chdir(project_dir)

    # Step 1: Pre-flight check - verify dist/ScreenshotterPro.exe is not locked by a running instance
    dist_exe = project_dir / "dist" / "ScreenshotterPro.exe"
    if dist_exe.exists():
        try:
            with open(dist_exe, "a+"):
                pass
        except PermissionError:
            print("\n" + "=" * 65)
            print("[ERROR] 'dist/ScreenshotterPro.exe' is currently running or locked by Windows!")
            print("Please close the running Screenshotter Pro app and re-run python build_exe.py.")
            print("=" * 65 + "\n")
            return

    # Step 2: Ensure PyInstaller is installed
    try:
        import PyInstaller
        print(f"[OK] PyInstaller found version {PyInstaller.__version__}")
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # Step 3: Ensure CustomTkinter assets are bundled correctly
    import customtkinter
    from PIL import Image

    ctk_path = Path(customtkinter.__file__).parent.resolve()

    # Step 4: Ensure .ico icon exists from PNG if not already generated
    ico_file = project_dir / "imgs" / "website_screenshotter_icon.ico"
    png_file = project_dir / "imgs" / "website_screenshotter_icon.png"

    if not ico_file.exists() and png_file.exists():
        try:
            print("Generating .ico from PNG...")
            img = Image.open(png_file)
            img.save(ico_file, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            print("[OK] Created imgs/website_screenshotter_icon.ico")
        except Exception as e:
            print(f"[WARN] Could not generate .ico: {e}")

    # Safely locate Tcl/Tk data directory if available
    tcl_candidates = [
        Path(sys.base_prefix) / "tcl",
        Path(sys.prefix) / "tcl",
        Path(sys.executable).parent / "tcl",
    ]
    tcl_path = next((p for p in tcl_candidates if p.exists()), None)

    print("Building standalone executable using PyInstaller...")

    # Build PyInstaller command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconsole",
        "--onefile",
        "--name=ScreenshotterPro",
        f"--paths={project_dir}",
        "--collect-all=customtkinter",
        "--collect-all=playwright",
        "--clean",
    ]

    # Add icon if available
    if ico_file.exists():
        cmd.append(f"--icon={ico_file}")
        cmd.append(f"--add-data={ico_file};imgs/")

    if png_file.exists():
        cmd.append(f"--add-data={png_file};imgs/")

    # Add hidden imports
    hidden_imports = [
        "customtkinter",
        "PIL",
        "PIL.ImageTk",
        "playwright",
        "playwright.async_api",
        "requests",
        "bs4",
        "asyncio",
        "urllib.request",
        "urllib.parse",
    ]
    for imp in hidden_imports:
        cmd.append(f"--hidden-import={imp}")

    if tcl_path and tcl_path.exists():
        cmd.extend([
            f"--add-data={tcl_path};_tcl_data/",
            f"--add-data={tcl_path};_tk_data/",
        ])

    cmd.append("website_screenshotter.py")

    print("Executing command:\n", " ".join(cmd), "\n")
    res = subprocess.run(cmd)

    if res.returncode == 0:
        exe_path = project_dir / "dist" / "ScreenshotterPro.exe"
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            print("\n" + "=" * 55)
            print("[SUCCESS] Standalone Screenshotter Pro executable built:")
            print(f"   Location: {exe_path}")
            print(f"   Size: {size_mb:.2f} MB")
            print("=" * 55)
        else:
            print("Build completed, but output executable was not found.")
    else:
        print(f"[ERROR] Build failed with return code {res.returncode}")


if __name__ == "__main__":
    main()
