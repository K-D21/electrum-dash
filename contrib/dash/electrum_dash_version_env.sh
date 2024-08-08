#!/bin/bash

VERSION_STRING=(`grep ELECTRUM_VERSION electrum_dash/version.py`)
DASH_ELECTRUM_VERSION=${VERSION_STRING[2]}
DASH_ELECTRUM_VERSION=${DASH_ELECTRUM_VERSION#\'}
DASH_ELECTRUM_VERSION=${DASH_ELECTRUM_VERSION%\'}
export DASH_ELECTRUM_VERSION

APK_VERSION_STRING=(`grep APK_VERSION electrum_dash/version.py`)
DASH_ELECTRUM_APK_VERSION=${APK_VERSION_STRING[2]}
DASH_ELECTRUM_APK_VERSION=${DASH_ELECTRUM_APK_VERSION#\'}
DASH_ELECTRUM_APK_VERSION=${DASH_ELECTRUM_APK_VERSION%\'}
export DASH_ELECTRUM_APK_VERSION

APK_VERSION_CODE_SCRIPT='./contrib/dash/calc_version_code.py'
export DASH_ELECTRUM_VERSION_CODE=`$APK_VERSION_CODE_SCRIPT`

# Check is release
SIMPLIFIED_VERSION_PATTERN="^([^A-Za-z]+).*"
if [[ ${DASH_ELECTRUM_VERSION} =~ ${SIMPLIFIED_VERSION_PATTERN} ]]; then
    if [[ ${BASH_REMATCH[1]} == ${DASH_ELECTRUM_VERSION} ]]; then
        export IS_RELEASE=y
    fi
fi