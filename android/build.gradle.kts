plugins {
    id("com.android.application") version "8.13.2" apply false
    id("org.jetbrains.kotlin.android") version "2.3.20" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.3.20" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "2.3.20" apply false
    id("com.google.devtools.ksp") version "2.3.8" apply false
}

allprojects {
    dependencyLocking {
        lockAllConfigurations()
    }

    // AGP creates Unified Test Platform host configurations lazily. Keep the
    // patched transport stack scoped to those host tools; it must not enter
    // the application runtime classpaths.
    afterEvaluate {
        configurations.matching {
            it.name.startsWith("_internal-unified-test-platform-")
        }.configureEach {
            dependencies.add(
                project.dependencies.platform("io.netty:netty-bom:4.1.136.Final"),
            )
            dependencyConstraints.add(
                project.dependencies.constraints.create(
                    "com.google.protobuf:protobuf-java:3.25.5",
                ),
            )
            dependencyConstraints.add(
                project.dependencies.constraints.create(
                    "com.google.protobuf:protobuf-kotlin:3.25.5",
                ),
            )
        }
    }
}
