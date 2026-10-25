@echo off
setlocal

set "ANDROID_DIR=%~dp0android-app"
set "APK_PATH=%ANDROID_DIR%\app\build\outputs\apk\dev\app-dev.apk"

if defined JAVA_HOME if not exist "%JAVA_HOME%\bin\java.exe" set "JAVA_HOME="
if not defined JAVA_HOME for /d %%J in ("%ProgramFiles%\Eclipse Adoptium\jdk-*") do if not defined JAVA_HOME if exist "%%~fJ\bin\java.exe" set "JAVA_HOME=%%~fJ"
if not defined JAVA_HOME for /d %%J in ("%ProgramFiles%\Java\jdk*") do if not defined JAVA_HOME if exist "%%~fJ\bin\java.exe" set "JAVA_HOME=%%~fJ"
if not defined JAVA_HOME if exist "%ProgramFiles%\Android\Android Studio\jbr\bin\java.exe" set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
if not defined JAVA_HOME if exist "%LOCALAPPDATA%\Programs\Android Studio\jbr\bin\java.exe" set "JAVA_HOME=%LOCALAPPDATA%\Programs\Android Studio\jbr"
if not defined JAVA_HOME for /f "delims=" %%J in ('where.exe java 2^>nul') do if not defined JAVA_HOME for %%K in ("%%~dpJ..") do set "JAVA_HOME=%%~fK"
if not defined JAVA_HOME (
    echo ERROR: Java was not found. Install JDK 17 or newer, or set JAVA_HOME.
    exit /b 1
)
set "PATH=%JAVA_HOME%\bin;%PATH%"

if not defined ANDROID_HOME if exist "%LOCALAPPDATA%\Android\Sdk" set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
if not defined ANDROID_SDK_ROOT if defined ANDROID_HOME set "ANDROID_SDK_ROOT=%ANDROID_HOME%"

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
