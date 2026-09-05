$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$cacheRoot = Join-Path $env:TEMP "onmyoji-motion-acl-v1.2.1"
$archive = Join-Path $env:TEMP "onmyoji-motion-acl-v1.2.1.zip"
$aclRoot = Join-Path $cacheRoot "acl-1.2.1"

if (-not (Test-Path (Join-Path $aclRoot "includes\acl\core\compressed_clip.h"))) {
    Invoke-WebRequest "https://github.com/nfrechette/acl/archive/refs/tags/v1.2.1.zip" -OutFile $archive
    Expand-Archive -LiteralPath $archive -DestinationPath $cacheRoot -Force
}

$vsRoot = "C:\Program Files\Microsoft Visual Studio\18\Community"
$vcvars = Join-Path $vsRoot "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path $vcvars)) {
    throw "未找到 Visual Studio C++ 编译工具。请安装 Visual Studio 的‘使用 C++ 的桌面开发’组件。"
}

$source = Join-Path $projectRoot "native\acl_v121_decoder.cpp"
$outputDir = Join-Path $projectRoot "tools"
$output = Join-Path $outputDir "onmyoji_acl_decode.exe"
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$object = Join-Path $cacheRoot "acl_v121_decoder.obj"
$command = '"{0}" && cl /nologo /std:c++17 /O2 /EHsc /I"{1}" /Fo:"{2}" /Fe:"{3}" "{4}"' -f $vcvars, (Join-Path $aclRoot "includes"), $object, $output, $source
cmd /d /s /c $command
if ($LASTEXITCODE -ne 0) { throw "动作解码器编译失败：$LASTEXITCODE" }
Write-Host "构建完成：$output"
