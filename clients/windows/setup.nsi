Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma
!include "MUI2.nsh"
!include "FileFunc.nsh"
!define PRODUCT "Дашборд Транстехком"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\TranstehcomDashboard"
Name "${PRODUCT}"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\Transtehcom\Dashboard"
Icon "${STAGE}\dashboard.ico"
UninstallIcon "${STAGE}\dashboard.ico"
VIProductVersion "1.0.0.0"
VIAddVersionKey /LANG=1049 "ProductName" "${PRODUCT}"
VIAddVersionKey /LANG=1049 "FileDescription" "Установка приложения Дашборд Транстехком"
VIAddVersionKey /LANG=1049 "FileVersion" "1.0.0"
VIAddVersionKey /LANG=1049 "LegalCopyright" "Транстехком"
!define MUI_ICON "${STAGE}\dashboard.ico"
!define MUI_UNICON "${STAGE}\dashboard.ico"
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\Dashboard.exe"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Section "Дашборд"
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File "${STAGE}\Dashboard.exe"
  File "${STAGE}\Microsoft.Web.WebView2.Core.dll"
  File "${STAGE}\Microsoft.Web.WebView2.WinForms.dll"
  File "${STAGE}\WebView2Loader.dll"
  File "${STAGE}\WebView2-LICENSE.txt"
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/EXTRACT" $R1
  IfErrors integrate done
  integrate:
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\${PRODUCT}.lnk" "$INSTDIR\Dashboard.exe"
  CreateShortcut "$DESKTOP\${PRODUCT}.lnk" "$INSTDIR\Dashboard.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "1.0.0"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Транстехком"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\Dashboard.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  done:
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\${PRODUCT}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT}.lnk"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  Delete "$INSTDIR\Dashboard.exe"
  Delete "$INSTDIR\Microsoft.Web.WebView2.Core.dll"
  Delete "$INSTDIR\Microsoft.Web.WebView2.WinForms.dll"
  Delete "$INSTDIR\WebView2Loader.dll"
  Delete "$INSTDIR\WebView2-LICENSE.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
