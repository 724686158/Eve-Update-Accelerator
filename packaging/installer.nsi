; EVE Update Accelerator —— Windows 10 (x64) 安装包脚本
; 用 NSIS 3.x 编译：makensis /DAPP_VERSION=1.3.0 /DOUTFILE=... packaging\installer.nsi
;
; 设计取舍：
;   * 装到 Program Files，所有用户共用（应用要改系统 hosts，天然是多用户级的东西）；
;   * 安装包本身提权（写 Program Files 必需），但不在卸载时碰 hosts——
;     用户可能正靠那几行 hosts 跑更新，卸载不该顺手把人网络改了；
;   * 不写 PATH：HKLM 的 PATH 可能超过 NSIS 的字符串上限，写坏一个 PATH
;     的代价远大于「命令行工具要自己进安装目录」这点不方便。

Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"

; ---- 版本与标识（可由命令行 /D 覆盖）----
!ifndef APP_VERSION
  !define APP_VERSION "1.3.0"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\dist\EveUpdateAccelerator-${APP_VERSION}-win10-x64-setup.exe"
!endif

!define APP_NAME      "EVE Update Accelerator"
!define APP_NAME_CN   "EVE 更新加速器"
!define APP_PUBLISHER "mengzicheng"
!define APP_URL       "https://github.com/724686158/Eve-Update-Accelerator"
!define APP_GUI_EXE   "eve-update-accelerator-gui.exe"
!define APP_CLI_EXE   "eve-update-accelerator.exe"
!define UNINST_KEY    "Software\Microsoft\Windows\CurrentVersion\Uninstall\EveUpdateAccelerator"

Name "${APP_NAME_CN} ${APP_VERSION}"
BrandingText "${APP_NAME} ${APP_VERSION}   ·   ${APP_URL}"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\EVE Update Accelerator"
InstallDirRegKey HKLM "${UNINST_KEY}" "InstallLocation"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
SetCompressorDictSize 32
CRCCheck on
XPStyle on

; setup.exe 自己的文件属性
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName"     "${APP_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME_CN} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion"     "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion"  "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "LegalCopyright"  "MIT License"

; ---- 界面 ----
!define MUI_ABORTWARNING
!define MUI_ICON   "..\assets\icon.ico"
!define MUI_UNICON "..\assets\icon.ico"

!define MUI_WELCOMEPAGE_TITLE "安装 ${APP_NAME_CN} ${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "这个工具给 EVE Online 的更新下载挑一条真正能下到数据的 CDN 落点，$\r$\n帮你把更新速度拉起来。$\r$\n$\r$\n它会做的事：探测候选落点 → 校验资源内容 → 实测吞吐，$\r$\n把确认可用的结果写进系统 hosts 的标记块里，随时可以一键撤销。$\r$\n$\r$\n它不改动任何游戏文件，不注入进程，也不绕过任何认证。$\r$\n$\r$\n点击「下一步」继续。"
!define MUI_LICENSEPAGE_TEXT_TOP "许可协议"
!define MUI_LICENSEPAGE_TEXT_BOTTOM "本工具以 MIT 协议发布，与 CCP hf. 无关联。"
!define MUI_DIRECTORYPAGE_TEXT_TOP "选择安装位置。默认装到 Program Files，所有用户都能用。"

!define MUI_FINISHPAGE_TITLE "安装完成"
!define MUI_FINISHPAGE_TEXT "${APP_NAME_CN} ${APP_VERSION} 已安装。$\r$\n$\r$\n首次使用建议先点「检查」：那是纯只读的，不会动你的 hosts；$\r$\n确认结果满意后再点「应用」，那一步才需要管理员权限。$\r$\n$\r$\n卸载时不会自动还原 hosts —— 如果你想恢复，先在程序里点「撤销」。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_GUI_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APP_NAME_CN}"
!define MUI_FINISHPAGE_LINK "项目主页 / 使用说明"
!define MUI_FINISHPAGE_LINK_LOCATION "${APP_URL}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; =====================================================================
; 初始化：升级安装时先确认程序没在运行
; =====================================================================
Function .onInit
  ; $PROGRAMFILES64 在 32 位 Windows 上是空字符串，直接拼出来的路径会变成
  ; 盘符根目录下的 "EVE Update Accelerator"，装进去就是个笑话。先挡住。
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK \
      "本安装包只支持 64 位 Windows。$\r$\n$\r$\n你的系统是 32 位的，请改用源码方式运行（见 README 的方式一）。"
    Abort
  ${EndIf}

  ; 用「能否以追加方式打开主程序文件」来判断它是否正被占用。
  ; 比解析 tasklist 的输出可靠——那玩意儿的提示文字随系统语言变。
  ${If} ${FileExists} "$INSTDIR\${APP_GUI_EXE}"
    ClearErrors
    FileOpen $0 "$INSTDIR\${APP_GUI_EXE}" a
    ${If} ${Errors}
      MessageBox MB_ICONSTOP|MB_OK \
        "${APP_NAME_CN} 正在运行。$\r$\n$\r$\n请先关闭它（包括任务栏里的窗口），然后再运行本安装程序。"
      Abort
    ${Else}
      FileClose $0
    ${EndIf}
  ${EndIf}

  ; 从控制面板「卸载」进来时 InstallDir 读的是注册表，这里兜个底
  ${If} $INSTDIR == ""
    StrCpy $INSTDIR "$PROGRAMFILES64\EVE Update Accelerator"
  ${EndIf}
FunctionEnd

; =====================================================================
; 主程序
; =====================================================================
Section "主程序（图形界面 + 命令行）" SEC_MAIN
  SectionIn RO
  ; 装进 Program Files 就意味着「所有用户」，快捷方式也要落在所有用户下，
  ; 否则第二个用户登录后开始菜单里什么都没有。
  SetShellVarContext all
  SetOutPath "$INSTDIR"

  ; dist\app 里是 PyInstaller 的 onedir 产物：两个 exe 共用一个 _internal 运行时。
  ; 不用 --onefile 是有意的：单文件每次启动都要把 30 MB 解压到临时目录，
  ; 启动慢、还容易被杀软拦——安装包已经把文件铺开了，没必要再自己解压一次。
  File /r "..\dist\app\*.*"

  File "..\LICENSE"
  File "..\README.md"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP_NAME_CN}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME_CN}\${APP_NAME_CN}.lnk" "$INSTDIR\${APP_GUI_EXE}" "" "$INSTDIR\${APP_GUI_EXE}" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME_CN}\卸载 ${APP_NAME_CN}.lnk" "$INSTDIR\Uninstall.exe"

  ; 控制面板 → 程序和功能
  WriteRegStr   HKLM "${UNINST_KEY}" "DisplayName"     "${APP_NAME_CN} (${APP_NAME})"
  WriteRegStr   HKLM "${UNINST_KEY}" "DisplayVersion"  "${APP_VERSION}"
  WriteRegStr   HKLM "${UNINST_KEY}" "Publisher"       "${APP_PUBLISHER}"
  WriteRegStr   HKLM "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\${APP_GUI_EXE},0"
  WriteRegStr   HKLM "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKLM "${UNINST_KEY}" "URLInfoAbout"    "${APP_URL}"
  WriteRegStr   HKLM "${UNINST_KEY}" "HelpLink"        "${APP_URL}"
  WriteRegStr   HKLM "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKLM "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKLM "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "创建桌面快捷方式" SEC_DESKTOP
  SetShellVarContext all
  CreateShortCut "$DESKTOP\${APP_NAME_CN}.lnk" "$INSTDIR\${APP_GUI_EXE}" "" "$INSTDIR\${APP_GUI_EXE}" 0
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_MAIN}    "图形界面、命令行工具与运行库，共用一个内置的 Python 运行时（无需另装 Python）。"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} "在桌面放一个启动图标。"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; =====================================================================
; 卸载
; =====================================================================
Section "Uninstall"
  SetShellVarContext all
  ; 卸载同样要求程序先退出，否则 _internal 里的 DLL 删不掉
  ${If} ${FileExists} "$INSTDIR\${APP_GUI_EXE}"
    ClearErrors
    FileOpen $0 "$INSTDIR\${APP_GUI_EXE}" a
    ${If} ${Errors}
      MessageBox MB_ICONSTOP|MB_OK \
        "${APP_NAME_CN} 正在运行，请先关闭它再卸载。"
      Abort
    ${Else}
      FileClose $0
    ${EndIf}
  ${EndIf}

  ; 注意：这里刻意不动 hosts。用户可能还指着它跑更新。
  Delete "$SMPROGRAMS\${APP_NAME_CN}\${APP_NAME_CN}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME_CN}\卸载 ${APP_NAME_CN}.lnk"
  RMDir  "$SMPROGRAMS\${APP_NAME_CN}"
  Delete "$DESKTOP\${APP_NAME_CN}.lnk"

  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APP_GUI_EXE}"
  Delete "$INSTDIR\${APP_CLI_EXE}"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKLM "${UNINST_KEY}"
SectionEnd
