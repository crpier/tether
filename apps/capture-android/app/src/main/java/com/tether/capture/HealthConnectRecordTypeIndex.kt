package com.tether.capture

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

interface HealthConnectRecordTypeIndex {
    fun remember(records: List<Pair<HealthConnectRecordType, String>>)

    fun find(recordId: String): HealthConnectRecordType?
}

class InMemoryHealthConnectRecordTypeIndex : HealthConnectRecordTypeIndex {
    private val types = mutableMapOf<String, HealthConnectRecordType>()

    override fun remember(records: List<Pair<HealthConnectRecordType, String>>) {
        records.forEach { (type, id) -> types[id] = type }
    }

    override fun find(recordId: String): HealthConnectRecordType? = types[recordId]
}

class SqliteHealthConnectRecordTypeIndex(context: Context) :
    SQLiteOpenHelper(context.applicationContext, DATABASE_NAME, null, DATABASE_VERSION),
    HealthConnectRecordTypeIndex {
    override fun onCreate(database: SQLiteDatabase) {
        database.execSQL(
            "CREATE TABLE health_connect_record_type (record_id TEXT PRIMARY KEY, record_type TEXT NOT NULL)",
        )
    }

    override fun onUpgrade(database: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    override fun remember(records: List<Pair<HealthConnectRecordType, String>>) {
        if (records.isEmpty()) return
        writableDatabase.beginTransaction()
        try {
            val statement = writableDatabase.compileStatement(
                "INSERT OR REPLACE INTO health_connect_record_type(record_id, record_type) VALUES (?, ?)",
            )
            records.forEach { (type, id) ->
                statement.clearBindings()
                statement.bindString(1, id)
                statement.bindString(2, type.wireName)
                statement.executeInsert()
            }
            writableDatabase.setTransactionSuccessful()
        } finally {
            writableDatabase.endTransaction()
        }
    }

    override fun find(recordId: String): HealthConnectRecordType? {
        readableDatabase.query(
            "health_connect_record_type",
            arrayOf("record_type"),
            "record_id = ?",
            arrayOf(recordId),
            null,
            null,
            null,
            "1",
        ).use { cursor ->
            if (!cursor.moveToFirst()) return null
            val wireName = cursor.getString(0)
            return HealthConnectRecordType.entries.firstOrNull { it.wireName == wireName }
        }
    }

    companion object {
        private const val DATABASE_NAME = "health_connect_record_types.sqlite3"
        private const val DATABASE_VERSION = 1
    }
}
