"""Release gates: upgrade paths, packaging, permissions and frozen capability surfaces (ADR-0032).

These tests are slower and broader than the integration suite on purpose. They answer the questions
a version 1 release has to answer — does every historical schema still upgrade, is the built
artifact installable, are the private files private, is the daemon single-instance, do the
capability surfaces stay frozen — and they use temporary XDG roots and fake external adapters, never
the network.
"""
