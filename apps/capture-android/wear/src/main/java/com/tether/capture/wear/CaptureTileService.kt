package com.tether.capture.wear

import androidx.wear.protolayout.ActionBuilders
import androidx.wear.protolayout.DimensionBuilders
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.ResourceBuilders
import androidx.wear.protolayout.TimelineBuilders
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import androidx.wear.tiles.TileService
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

private const val RESOURCES_VERSION = "1"
private const val CLICKABLE_ID = "open_recording_activity"

/**
 * Tiles have no gesture handlers (ProtoLayout supports only a discrete tap
 * click, never hold/release), so this tile's only job is a single tappable
 * surface that launches [RecordingActivity]; the actual record/upload UX
 * lives entirely in that activity.
 */
class CaptureTileService : TileService() {
    override fun onTileRequest(
        requestParams: RequestBuilders.TileRequest,
    ): ListenableFuture<TileBuilders.Tile> {
        val timeline =
            TimelineBuilders.Timeline.Builder()
                .addTimelineEntry(
                    TimelineBuilders.TimelineEntry.Builder()
                        .setLayout(
                            LayoutElementBuilders.Layout.Builder()
                                .setRoot(tileLayout(packageName, getString(R.string.tile_label)))
                                .build(),
                        )
                        .build(),
                )
                .build()

        val tile =
            TileBuilders.Tile.Builder()
                .setResourcesVersion(RESOURCES_VERSION)
                .setTileTimeline(timeline)
                .build()
        return Futures.immediateFuture(tile)
    }

    override fun onTileResourcesRequest(
        requestParams: RequestBuilders.ResourcesRequest,
    ): ListenableFuture<ResourceBuilders.Resources> {
        val resources = ResourceBuilders.Resources.Builder().setVersion(RESOURCES_VERSION).build()
        return Futures.immediateFuture(resources)
    }
}

/**
 * The whole tile: a full-surface tap target that launches the recording
 * activity, labeled with [label]. Kept as a pure function over plain values
 * (no Context/Service dependency beyond the two strings) so it's callable
 * from a JVM test if an assertion on it ever pays its way.
 */
fun tileLayout(packageName: String, label: String): LayoutElementBuilders.LayoutElement {
    val launchRecording =
        ActionBuilders.LaunchAction.Builder()
            .setAndroidActivity(
                ActionBuilders.AndroidActivity.Builder()
                    .setPackageName(packageName)
                    .setClassName(RecordingActivity::class.java.name)
                    .build(),
            )
            .build()

    val clickable =
        ModifiersBuilders.Clickable.Builder()
            .setId(CLICKABLE_ID)
            .setOnClick(launchRecording)
            .build()

    return LayoutElementBuilders.Box.Builder()
        .setWidth(DimensionBuilders.expand())
        .setHeight(DimensionBuilders.expand())
        .setModifiers(
            ModifiersBuilders.Modifiers.Builder()
                .setClickable(clickable)
                .build(),
        )
        .addContent(LayoutElementBuilders.Text.Builder().setText(label).build())
        .build()
}
