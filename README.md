# Saqar KNX

Android app for the in-house wall screen / tablet. It runs the KNX bridge
(`app/src/main/python/knx_bridge.py`) as a background service that talks to the
KNX IP router on the house LAN and keeps a connection to the company relay server,
so the house is reachable at `https://knx.saqar-akd.com.iq/h/<house>/`.

Build: GitHub Actions (`.github/workflows/build.yml`) produces the APK as a build artifact.
