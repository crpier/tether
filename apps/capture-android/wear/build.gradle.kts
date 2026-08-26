plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.tether.capture.wear"
    compileSdk = 36
    buildToolsVersion = "36.1.0"

    defaultConfig {
        applicationId = "com.tether.capture.wear"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }

    lint {
        // The app builds headless with no CI; fail the build on real problems
        // but keep the baseline explicit so justified suppressions are visible.
        warningsAsErrors = false
        abortOnError = true
    }
}

dependencies {
    implementation(project(":core"))

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.datastore:datastore-preferences:1.1.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // Tile rendering: androidx.wear.tiles keeps TileService/TileBuilders in its
    // own package for compat; the actual layout/action/resource builders live
    // in androidx.wear.protolayout since the 1.1 split.
    implementation("androidx.wear.tiles:tiles:1.6.1")
    implementation("androidx.wear.protolayout:protolayout:1.4.1")
    // TileService callbacks are ListenableFuture; the tile is built
    // synchronously so Futures.immediateFuture is all that's needed.
    implementation("com.google.guava:guava:33.6.0-android")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
}
