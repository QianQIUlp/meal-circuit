pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        providers.environmentVariable("MEALCIRCUIT_LOCAL_MAVEN").orNull?.let {
            maven { url = uri(it) }
        }
        google()
        mavenCentral()
    }
}
rootProject.name = "MealCircuit"
include(":app")
