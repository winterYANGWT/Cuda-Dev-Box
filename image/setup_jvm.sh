#!/bin/bash
set -euo pipefail

: "${KOTLIN_VERSION:?KOTLIN_VERSION must be set}"

# Install the standalone Kotlin compiler directly in the final development image.
curl -fsSL --retry 3 \
    "https://github.com/JetBrains/kotlin/releases/download/v${KOTLIN_VERSION}/kotlin-compiler-${KOTLIN_VERSION}.zip" \
    -o /tmp/kotlin.zip
rm -rf /opt/kotlin /opt/kotlinc
unzip -q /tmp/kotlin.zip -d /opt
mv /opt/kotlinc /opt/kotlin
rm /tmp/kotlin.zip

ln -sf /opt/maven/bin/mvn /usr/local/bin/mvn
ln -sf /opt/gradle/bin/gradle /usr/local/bin/gradle
for binary in kotlin kotlinc kotlinc-jvm kotlinc-js kapt; do
    if [ -x "/opt/kotlin/bin/$binary" ]; then
        ln -sf "/opt/kotlin/bin/$binary" "/usr/local/bin/$binary"
    fi
done
