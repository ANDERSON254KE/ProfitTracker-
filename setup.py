import os
import subprocess
import sys

def setup():
    print("=== PROFIT TRACKER SETUP ===")
    
    # 1. Create virtual environment
    if not os.path.exists('venv'):
        print("Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", "venv"])
    else:
        print("Virtual environment already exists.")

    # 2. Determine pip path
    if os.name == 'nt':
        pip_path = os.path.join('venv', 'Scripts', 'pip.exe')
    else:
        pip_path = os.path.join('venv', 'bin', 'pip')

    # 3. Install requirements
    if os.path.exists('requirements.txt'):
        print("Installing dependencies from requirements.txt...")
        subprocess.run([pip_path, "install", "-r", "requirements.txt"])
    else:
        print("Error: requirements.txt not found.")

    print("\n=== SETUP COMPLETE ===")
    print("\nTo activate the environment and run the dashboard:")
    if os.name == 'nt':
        print("1. venv\\Scripts\\activate")
    else:
        print("1. source venv/bin/activate")
    print("2. python dashboard.py")

if __name__ == "__main__":
    setup()
