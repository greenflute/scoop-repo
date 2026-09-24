# Scoop bucket

A personal Scoop bucket for Tokcos and other useful Windows applications.

## Add the bucket

```powershell
scoop bucket add greenflute https://github.com/greenflute/scoop-repo
```

## Install Tokcos

```powershell
scoop install greenflute/tokcos-cli
scoop install greenflute/tokcos-work
```

Other applications:

```powershell
scoop install greenflute/aardio
scoop install greenflute/moonbit
```

The manifests use versioned official Tokcos downloads and fixed SHA256 checksums.

The official MoonBit VS Code extension currently looks for its toolchain under
`%MOON_HOME%\bin` (default: `%USERPROFILE%\.moon\bin`) instead of using commands
installed through Scoop, so it does not automatically discover this installation. For
VS Code use, let the official extension manage its own toolchain for now.
