-- SyncSonic: force all Bluetooth nodes to low driver priority so
-- virtual_out (null-sink) always wins the PipeWire graph clock election.
-- Without this, a bluez_output A2DP sink (default priority.driver=1010)
-- becomes the graph timing master, and any BT transport jitter stalls
-- the entire audio pipeline — causing audible dropouts on all speakers.
--
-- This rule must agree with the priority.driver value baked into the
-- module-null-sink load in syncsonic_ble/helpers/pulseaudio_helpers.py
-- (currently 10000). The rule sets bluez nodes to 100 so virtual_out
-- wins the election with a wide margin even if BlueZ defaults change.

bluez_monitor.rules = {
  -- Preserve the stock card-level rule from 50-bluez-config.lua
  {
    matches = {
      {
        { "device.name", "matches", "bluez_card.*" },
      },
    },
    apply_properties = {
      ["bluez5.auto-connect"] = "[ hfp_hf hsp_hs a2dp_sink ]",
    },
  },
  -- Override: all Bluetooth audio nodes get low driver priority
  {
    matches = {
      {
        { "node.name", "matches", "bluez_input.*" },
      },
      {
        { "node.name", "matches", "bluez_output.*" },
      },
    },
    apply_properties = {
      ["priority.driver"]     = 100,
      ["priority.session"]    = 100,
      ["node.pause-on-idle"]  = false,
    },
  },
}
