plugins {
    kotlin("jvm")
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    // `api` so consumers (app, wear) get OkHttp request/response types on their
    // own compile classpath without redeclaring the dependency.
    api("com.squareup.okhttp3:okhttp:4.12.0")
    // Android provides a real org.json implementation at runtime (only its unit
    // test stubs throw); compileOnly here mirrors that — never packaged, and
    // Android consumers already have it on their platform classpath.
    compileOnly("org.json:json:20240303")

    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    // Plain JVM tests have no Android stub to fall back on: pull in the real
    // org.json implementation so request-building tests exercise real JSON.
    testImplementation("org.json:json:20240303")
}
