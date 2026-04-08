# Neutral Foundation Notes

This branch keeps only the minimal PipeWire delay-node core needed for the
Bluetooth-only neutral foundation.

The following PipeWire modules were intentionally removed from the foundation
branch and are reserved as Epic 1 reintroduction/reference material:

- `syncsonic_ble/helpers/pipewire_alignment_maintenance.py`
- `syncsonic_ble/helpers/pipewire_observability.py`
- `syncsonic_ble/helpers/pipewire_profiler_monitor.py`
- `syncsonic_ble/helpers/pipewire_dsp_contract.py`
- `syncsonic_ble/helpers/pipewire_dsp_node_manager.py`
- `syncsonic_ble/helpers/pipewire_dsp_runtime.py`
- `syncsonic_ble/helpers/pipewire_filter_chain_driver.py`
- `syncsonic_ble/helpers/pipewire_graph_applier.py`
- `syncsonic_ble/helpers/pipewire_graph_executor.py`
- `syncsonic_ble/helpers/pipewire_graph_launcher.py`
- `syncsonic_ble/helpers/pipewire_processor_runtime.py`

They are not considered discarded. When Epic 1 starts, review these modules
before mining older branches so any useful transport and stability ideas can be
reintroduced deliberately.
