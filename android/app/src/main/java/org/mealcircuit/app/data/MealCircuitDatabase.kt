package org.mealcircuit.app.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [
        AppMetadataEntity::class,
        DomainRevisionEntity::class,
        EntityHeadEntity::class,
        MaterializedRecordEntity::class,
        SyncOutboxEntity::class,
        SyncShadowEntity::class,
        SyncConflictEntity::class,
        UnknownEntity::class,
        ManagedAssetEntity::class,
        SyncConfigurationEntity::class,
    ],
    version = 2,
    exportSchema = true,
)
abstract class MealCircuitDatabase : RoomDatabase() {
    abstract fun dao(): MealCircuitDao

    companion object {
        @Volatile private var instance: MealCircuitDatabase? = null

        fun open(context: Context): MealCircuitDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(
                context.applicationContext,
                MealCircuitDatabase::class.java,
                "mealcircuit.db",
            ).addMigrations(MIGRATION_1_2).build().also { instance = it }
        }

        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("CREATE TABLE IF NOT EXISTS `app_metadata` (`key` TEXT NOT NULL, `value` TEXT NOT NULL, PRIMARY KEY(`key`))")
                db.execSQL("INSERT OR REPLACE INTO `app_metadata` (`key`,`value`) VALUES ('schema_version','2')")
            }
        }
    }
}
