# This script activates the venv and then runs the main.py:
#!/bin/bash

# get the current location of the script:
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

#use the location to navigate to the project root:
cd "$SCRIPT_DIR"

# Activate the virtual environment and run the main.py script
source .venv/bin/activate
python main.py