# Installing MouseShare on Windows

Download `mouseshare.exe` from
[Releases](https://github.com/ArcSoftLabs/mouseshare/releases) and run it.

Windows 11 and updated Windows 10 installations normally include the required
WebView2 runtime. If MouseShare opens without a usable window, install the
current Microsoft Edge WebView2 Runtime and try again.

The build is unsigned, so SmartScreen may warn on first launch. Choose **More
info** and **Run anyway** only if the file came from the MouseShare releases
page. When Windows Firewall asks, allow MouseShare on the networks where your
other devices are connected. Discovery uses UDP 5353 and sessions use TCP
39471 by default.

To connect, launch MouseShare on both devices and press **Connect** on the
Windows machine if its keyboard and pointer should be shared. If the other
machine should provide the input, press Connect there instead. Enter the
six-digit code displayed by the receiving machine.

The configuration file is `%USERPROFILE%\.mouseshare\config.json`. Closing the
MouseShare window exits the application; minimize it to keep sharing.

