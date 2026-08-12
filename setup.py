import subprocess
import sys

print("Installing requirements...")
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)

# Ensure tray support is available (pystray) and Pillow is installed for icon handling
print("Installing optional tray dependencies (pystray, pillow)...")
subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "pillow"], check=True)

print("Installing Playwright browsers...")
subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

print("\n✅ Setup complete! Run 'python main.py' to start JARVIS.")

