# FT8Commander-NG

> This is an experimental piece of code. Don't forget to run `git pull` often.
> This code only works with the version of WSJT-X 2.5 and above.

### WSJT-X FT8 Automation

FT8Commander is an experimental project for ham radio operators who
want automatic control of their FT8 contacts. This program controls
WSJT-X to optimize contacts' chances during a contest or DX (make as
many QSO as possible). After a receive sequence, the program uses
information such as the SNR[^1] and the distance of the calling
stations to calculate which one has the most chances of completing the
QSO.

## Usage:
  1. If you receive an error `ModuleNotFoundError: No module named 'yaml'`, it can be resolved by installing the `pyyaml` package: `pip install pyyaml`
  2. Start WSJT-X
  3. In a terminal or powershell Go to the directory FT8Commander
  4. Copy the `ft8ctrl.yaml.sample` into `ft8ctrl.yaml`
  5. Edit the configuration file and enter your information
  6. Start the Python program:
   - On Linux or MacOS type `./ft8ctrl.py`
   - On Windows, in command mode or powershell type `python .\ft8ctl.py`
  7. Watch WSJT-X making contacts.

> This program runs on MacOS and Linux.

[^1]: Signal To Noise Ratio
