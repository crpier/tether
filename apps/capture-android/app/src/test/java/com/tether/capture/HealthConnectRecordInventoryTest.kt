package com.tether.capture

import androidx.health.connect.client.records.Record
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.util.jar.JarFile

class HealthConnectRecordInventoryTest {
    @Test
    fun inventoryNamesEveryConcreteSdkRecordType() {
        assertEquals(
            sdkRecordTypeNames(),
            HealthConnectRecordInventory.entries.map { it.recordClass.simpleName }.toSet(),
        )
    }

    private fun sdkRecordTypeNames(): Set<String> {
        val location = checkNotNull(Record::class.java.protectionDomain).codeSource.location
        val path = URLDecoder.decode(location.path, StandardCharsets.UTF_8.name())
        val file = File(path)
        val names = if (file.isDirectory) {
            file.walkTopDown()
                .filter { it.isFile && it.path.endsWith("Record.class") }
                .map { it.name.removeSuffix(".class") }
                .toSet()
        } else {
            JarFile(file).use { jar ->
                jar.entries().asSequence()
                    .map { it.name }
                    .filter { it.startsWith("androidx/health/connect/client/records/") }
                    .filter { it.endsWith("Record.class") }
                    .map { it.substringAfterLast('/').removeSuffix(".class") }
                    .toSet()
            }
        }
        return names - setOf("Record", "InstantaneousRecord", "IntervalRecord", "SeriesRecord")
    }
}
