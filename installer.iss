; Stocklens 安装包脚本（Inno Setup 6）
; 用途：把 Nuitka standalone 产物打包为单个安装程序，
;       安装后创建桌面 / 开始菜单快捷方式，用户双击快捷方式即可启动（启动无自解压、更快）。
;
; 前置：先用 tools\build_v3_nuitka.py 打出 standalone 产物。
; 编译：用 Inno Setup 的 ISCC.exe 编译本文件，或运行 生成安装包.bat。

#ifndef MyAppVersion
  #error "tools/build_v3_nuitka.py must supply MyAppVersion from release_identity.py"
#endif
#ifndef MyAppName
  #error "tools/build_v3_nuitka.py must supply MyAppName from release_identity.py"
#endif
#ifndef MyPublisher
  #error "tools/build_v3_nuitka.py must supply MyPublisher from release_identity.py"
#endif
#ifndef MyAppExeName
  #error "tools/build_v3_nuitka.py must supply MyAppExeName from release_identity.py"
#endif
#ifndef MyDistDir
  #error "tools/build_v3_nuitka.py must supply MyDistDir from release_identity.py"
#endif
#ifndef ReleaseNoteFile
  #error "tools/build_v3_nuitka.py must supply ReleaseNoteFile from release_identity.py"
#endif
#ifndef InstallerBaseFilename
  #error "tools/build_v3_nuitka.py must supply InstallerBaseFilename from release_identity.py"
#endif
#ifndef InstallerOutputDir
  #error "tools/build_v3_nuitka.py must supply InstallerOutputDir from release_identity.py"
#endif
#ifndef MyDefaultDirName
  #error "tools/build_v3_nuitka.py must supply MyDefaultDirName from release_identity.py"
#endif

[Setup]
; AppId 唯一标识本程序，升级时保持不变（勿随意更改）。
AppId={{8F3C9A21-7B4E-4D6A-9C2E-A1B2C3D4E5F6}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyPublisher}
DefaultDirName={autopf}\{#MyDefaultDirName}
DefaultGroupName={#MyDefaultDirName}
DisableProgramGroupPage=yes
; 仅当前用户安装，无需管理员权限。
PrivilegesRequired=lowest
OutputDir={#InstallerOutputDir}
OutputBaseFilename={#InstallerBaseFilename}
SetupIconFile=assets\stocklens.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; 如需简体中文向导界面：先确认 Inno Setup 的 Languages 目录有 ChineseSimplified.isl
; （非官方语言包，需自行下载放入），再取消下一行注释并删除本段下方的英文 Languages。
; [Languages]
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; 递归打包整个 onedir 文件夹（含 Stocklens.exe 与 _internal 子目录）。
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "docs\买家使用说明.txt"; DestDir: "{app}"; DestName: "使用说明.txt"; Flags: ignoreversion
Source: "docs\免责声明.txt"; DestDir: "{app}"; DestName: "免责声明.txt"; Flags: ignoreversion
Source: "docs\常见问题.txt"; DestDir: "{app}"; DestName: "常见问题.txt"; Flags: ignoreversion
Source: "docs\示例策略_v1.json"; DestDir: "{app}"; DestName: "示例策略_v1.json"; Flags: ignoreversion
Source: "{#ReleaseNoteFile}"; DestDir: "{app}"; DestName: "版本说明.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
