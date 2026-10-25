@echo off
setlocal

set "ANDROID_DIR=%~dp0android-app"
set "APK_PATH=%ANDROID_DIR%\app\build\outputs\apk\dev\app-dev.apk"

if not exist "%ANDROID_DIR%\gradlew.bat" (
    echo ERROR: Could not find android-app\gradlew.bat.
    exit /b 1
)

pushd "%ANDROID_DIR%"
call gradlew.bat :app:assembleDev
set "BUILD_RESULT=%ERRORLEVEL%"
popd

if not "%BUILD_RESULT%"=="0" (
    echo ERROR: Dev build failed.
    exit /b %BUILD_RESULT%
)

if not exist "%APK_PATH%" (
    echo ERROR: Dev build completed but the APK was not found.
    exit /b 1
)

echo Dev APK: %APK_PATH%
