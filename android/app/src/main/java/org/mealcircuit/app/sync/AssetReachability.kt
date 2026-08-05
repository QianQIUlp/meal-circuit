package org.mealcircuit.app.sync

import java.util.ArrayDeque
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.mealcircuit.app.data.ManagedAssetEntity
import org.mealcircuit.app.domain.DomainRevision
import org.mealcircuit.app.domain.EntityKind

internal data class ManagedAssetReachability(
    val referenced: List<ManagedAssetEntity>,
    val unreferenced: List<ManagedAssetEntity>,
    val allForKeyRotation: List<ManagedAssetEntity>,
)

internal fun referencedAssetIds(revisions: Iterable<DomainRevision>): Set<String> {
    val referenced = linkedSetOf<String>()
    val pending = ArrayDeque<JsonElement>()
    revisions.forEach { revision ->
        if (revision.entityKind == EntityKind.ASSET) return@forEach
        pending.addLast(revision.payload)
        while (pending.isNotEmpty()) {
            when (val element = pending.removeLast()) {
                is JsonObject -> element.forEach { (key, value) ->
                    if (
                        key.endsWith("asset_id") &&
                        value is JsonPrimitive &&
                        value.isString
                    ) {
                        referenced += value.content
                    }
                    pending.addLast(value)
                }
                is JsonArray -> element.forEach(pending::addLast)
                else -> Unit
            }
        }
    }
    return referenced
}

internal fun classifyManagedAssetsByReachability(
    assets: List<ManagedAssetEntity>,
    referencedAssetIds: Set<String>,
): ManagedAssetReachability {
    val referenced = mutableListOf<ManagedAssetEntity>()
    val unreferenced = mutableListOf<ManagedAssetEntity>()
    assets.forEach { asset ->
        if (asset.id in referencedAssetIds) referenced += asset else unreferenced += asset
    }
    return ManagedAssetReachability(
        referenced = referenced,
        unreferenced = unreferenced,
        // Rotation commit cannot safely reduce the opaque remote blob inventory. Preserve every
        // locally managed blob so an unreferenced historical blob is not deleted during rotation.
        allForKeyRotation = assets.toList(),
    )
}
