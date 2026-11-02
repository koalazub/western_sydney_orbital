"""Ali's official-profile controller entrypoint.

The flight policy is shared from ``wsorb.controllers.official_profile`` so the
sweep and this named contribution cannot drift apart. Run it through the
official comparison with::

    just flight-sweep examples/official_comparison.json runs/ali-official-new

This is the local competition-reference policy. It is not the portable STP
checkpoint controller in ``examples/wso/``.
"""

from __future__ import annotations

from wsorb.controllers.official_profile import POLICY_ID, official_profile_policy

POLICY_FACTORY = official_profile_policy

__all__ = ["POLICY_FACTORY", "POLICY_ID", "official_profile_policy"]
