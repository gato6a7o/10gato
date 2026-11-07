# Preparar el ZIP portatil de 10gato con las versiones probadas en Windows x64
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FFmpegRoot,
    [string]$Python,
    [string]$OutputZip,
    [string]$Wheelhouse
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $Python) { $Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not $OutputZip) { $OutputZip = Join-Path $PSScriptRoot 'dist\10gato-windows-x64.zip' }

function Invoke-BuildPython {
    # Ejecutar cada paso de Python y detener la construccion si falla
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python termino con codigo $LASTEXITCODE al ejecutar el paso de construccion."
    }
}

$pythonVersion = '3.14.7'
$packages = @(
    'PyInstaller==6.22.3'
    'pyinstaller-hooks-contrib==2026.9'
    'PySide6-Essentials==6.12.0'
    'shiboken6==6.12.0'
    'altgraph==0.17.5'
    'packaging==26.3'
    'pefile==2024.8.26'
    'pywin32-ctypes==0.2.3'
    'setuptools==84.2.1'
)
$ffmpegHashes = [ordered]@{
    'ffmpeg.exe' = '3256173F3F8BFFD7DF12227C68ADF68025EDB1832273A9530688A7BB1ED8EDEC'
    'ffprobe.exe' = 'F0D36ECBBDD3BCFAC3EFA078C96C7271C2E68B3810595552AC3B7F17E9A65C52'
}
$lgplUrl = 'https://www.gnu.org/licenses/lgpl-3.0.txt'
$lgplHash = 'E3A994D82E644B03A792A930F574002658412F62407F5FEE083F2555C5F23118'

# Comprobar las entradas antes de instalar dependencias o generar archivos
$zipPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputZip)
if ([IO.Path]::GetExtension($zipPath) -ine '.zip' -or (Test-Path -LiteralPath $zipPath)) {
    throw "Indica un ZIP nuevo. No se reemplaza el destino: $zipPath"
}
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$platform = Invoke-BuildPython $pythonPath @('-I', '-B', '-c',
    'import platform,sysconfig; print(platform.python_version(), sysconfig.get_platform())')
if ($platform -ne "$pythonVersion win-amd64") {
    throw "Se necesita Python $pythonVersion para Windows x64. Detectado: $platform"
}
$pythonLicense = Invoke-BuildPython $pythonPath @('-I', '-B', '-c',
    "import sys; from pathlib import Path; print(Path(sys.base_prefix) / 'LICENSE.txt')")
$ffmpegDir = (Resolve-Path -LiteralPath $FFmpegRoot).Path
$assetsDir = Join-Path $PSScriptRoot 'assets'
$logo = Join-Path $assetsDir 'Julin_.png'
$mascot = Join-Path $assetsDir 'julin_(1).png'
$icon = Join-Path $assetsDir '10gato.ico'
$entryPoint = Join-Path $PSScriptRoot 'gui.py'
$ffmpegLicense = Join-Path $ffmpegDir 'LICENSE'
$ffmpegReadme = Join-Path $ffmpegDir 'README.txt'
foreach ($file in @($entryPoint, $logo, $mascot, $pythonLicense, $ffmpegLicense, $ffmpegReadme)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Falta $file" }
}
foreach ($name in $ffmpegHashes.Keys) {
    $file = Join-Path $ffmpegDir "bin\$name"
    if ((Get-FileHash -LiteralPath $file).Hash -ne $ffmpegHashes[$name]) {
        throw "$name no coincide con la compilacion Gyan 9.0.2 essentials esperada."
    }
}
if ($Wheelhouse) { $Wheelhouse = (Resolve-Path -LiteralPath $Wheelhouse).Path }

# Trabajar en una carpeta nueva junto al destino para publicar el ZIP completo de una vez
$outputDir = Split-Path -Parent $zipPath
[void][IO.Directory]::CreateDirectory($outputDir)
$buildName = '.10gato-build-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$buildDir = Join-Path $outputDir $buildName
[void](New-Item -ItemType Directory -Path $buildDir)
$previousCache = $env:PYINSTALLER_CONFIG_DIR
$previousBuildPath = $env:PATH
try {
    $lgplLicense = Join-Path $buildDir 'LGPL-3.0.txt'
    if ($Wheelhouse) {
        Copy-Item -LiteralPath (Join-Path $Wheelhouse 'LGPL-3.0.txt') -Destination $lgplLicense
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri $lgplUrl -OutFile $lgplLicense
    }
    if ((Get-FileHash -LiteralPath $lgplLicense).Hash -ne $lgplHash) {
        throw 'El texto de LGPLv3 no coincide con la copia oficial verificada.'
    }

    # Usar un entorno limpio con Essentials para evitar incorporar modulos de Qt ajenos a la ventana
    $venv = Join-Path $buildDir 'venv'
    Invoke-BuildPython $pythonPath @('-I', '-B', '-m', 'venv', $venv)
    $buildPython = Join-Path $venv 'Scripts\python.exe'
    $pipArgs = @('-I', '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check',
        '--no-cache-dir', 'install', '--only-binary=:all:')
    if ($Wheelhouse) { $pipArgs += @('--no-index', '--find-links', $Wheelhouse) }
    Invoke-BuildPython $buildPython ($pipArgs + $packages)
    Invoke-BuildPython $buildPython @('-I', '-B', '-m', 'pip', '--disable-pip-version-check', 'check')

    if (-not (Test-Path -LiteralPath $icon)) {
        # Crear un ICO con varios tamanos para el Explorador y el ejecutable
        $iconScript = @'
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter

source = QImage(sys.argv[1])
if source.isNull():
    raise SystemExit("No se pudo leer el PNG del icono.")

sizes = (16, 24, 32, 48, 64, 128, 256)
directory = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
images = bytearray()
offset = len(directory) + 16 * len(sizes)
for size in sizes:
    canvas = QImage(size, size, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    scaled = source.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    with QPainter(canvas) as painter:
        painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not canvas.save(buffer, "PNG"):
        raise SystemExit("No se pudo convertir el icono a PNG.")
    data = bytes(buffer.data())
    directory.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                                    len(data), offset + len(images)))
    images.extend(data)

with Path(sys.argv[2]).open("xb") as icon_file:
    icon_file.write(directory + images)
'@
        $iconScript | & $buildPython -I -B - $logo $icon
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear el icono.' }
    }

    # Colocar las imagenes donde gui.py las busca y preparar los binarios externos aparte
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $buildDir 'cache'
    # Limitar la busqueda de DLL del empaquetador a Windows y al entorno de construccion
    $env:PATH = (Join-Path $env:WINDIR 'System32') + ';' + $env:WINDIR
    $bundleParent = Join-Path $buildDir 'bundle'
    Invoke-BuildPython $buildPython @('-I', '-B', '-m', 'PyInstaller',
        '--onedir', '--windowed', '--noupx', '--name', '10gato', '--icon', $icon,
        '--add-data', "${logo}:assets", '--add-data', "${mascot}:assets",
        '--distpath', $bundleParent, '--workpath', (Join-Path $buildDir 'work'),
        '--specpath', $buildDir, $entryPoint)
    $bundle = Join-Path $bundleParent '10gato'
    $binDir = Join-Path $bundle 'bin'
    $licenses = Join-Path $bundle 'licenses'
    [void][IO.Directory]::CreateDirectory($binDir)
    [void][IO.Directory]::CreateDirectory($licenses)
    foreach ($name in $ffmpegHashes.Keys) {
        Copy-Item -LiteralPath (Join-Path $ffmpegDir "bin\$name") -Destination $binDir
    }

    Copy-Item -LiteralPath $ffmpegLicense -Destination (Join-Path $licenses 'FFmpeg-GPL-3.0.txt')
    Copy-Item -LiteralPath $ffmpegReadme -Destination (Join-Path $licenses 'FFmpeg-build.txt')
    Copy-Item -LiteralPath $lgplLicense -Destination $licenses
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenses 'Python-LICENSE.txt')
    $pyinstallerLicense = Join-Path $venv 'Lib\site-packages\pyinstaller-6.22.3.dist-info\licenses\COPYING.txt'
    Copy-Item -LiteralPath $pyinstallerLicense -Destination (Join-Path $licenses 'PyInstaller-COPYING.txt')
    @'
10gato para Windows x64

Extrae toda la carpeta y abre 10gato.exe. Mantener juntas las carpetas bin e _internal.
Selecciona o arrastra un clip. Si hay varias pistas de audio, elige la pista o mezcla.
El resultado verificado se guarda junto al original, con un nombre disponible.

FFmpeg y ffprobe 9.0.2 essentials de Gyan, GPLv3:
https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2
Revision de FFmpeg indicada por el proveedor:
https://github.com/FFmpeg/FFmpeg/commit/946fcce07b
Configuracion del proveedor en licenses/FFmpeg-build.txt.

Qt, PySide6 Essentials y Shiboken6 6.12.0, LGPLv3:
https://download.qt.io/official_releases/qt/6.12/6.12.0/
https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v6.12.0
https://doc.qt.io/qt-6/licenses-used-in-qt.html
https://doc.qt.io/qtforpython-6/licenses.html

Python 3.14.7:
https://www.python.org/downloads/release/python-3147/
PyInstaller 6.22.3:
https://github.com/pyinstaller/pyinstaller/tree/v6.22.3
'@ | Set-Content -LiteralPath (Join-Path $bundle 'LEEME.txt') -Encoding UTF8
    [ordered]@{
        python = $pythonVersion
        platform = 'win-amd64'
        buildPackages = $packages
        ffmpeg = '9.0.2-essentials_build-www.gyan.dev'
        ffmpegSha256 = $ffmpegHashes
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $bundle 'build-info.json') -Encoding UTF8

    # Comprimir la carpeta completa y publicar sin reemplazar un ZIP existente
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = Join-Path $buildDir '10gato.zip'
    [IO.Compression.ZipFile]::CreateFromDirectory($bundle, $archive, 'Optimal', $true)
    [IO.File]::Move($archive, $zipPath)
    Write-Output "ZIP listo: $zipPath"
}
finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousCache
    $env:PATH = $previousBuildPath
    # Borrar solo la carpeta temporal creada por esta ejecucion
    $resolvedBuildDir = (Resolve-Path -LiteralPath $buildDir).Path
    if ((Split-Path -Parent $resolvedBuildDir) -eq $outputDir -and
        (Split-Path -Leaf $resolvedBuildDir) -eq $buildName) {
        # Dar tiempo a que Windows libere los archivos recien generados
        for ($attempt = 0; $attempt -lt 5; $attempt++) {
            try {
                Remove-Item -LiteralPath $resolvedBuildDir -Recurse -Force
                break
            } catch {
                if ($attempt -eq 4) {
                    Write-Warning "Quedo una carpeta temporal bloqueada: $resolvedBuildDir"
                } else {
                    Start-Sleep -Milliseconds (250 * ($attempt + 1))
                }
            }
        }
    }
}
