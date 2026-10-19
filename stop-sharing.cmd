@echo off
cd /d "%~dp0"
if not exist ".hosting" mkdir ".hosting"
type nul > ".hosting\stop"
echo Sharing will stop in a few seconds.
