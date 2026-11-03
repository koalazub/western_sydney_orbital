// Navigation metadata only. Opening a link never grants flight consent.
import profiles from "../../src/wsorb/controllers/catalog.json" with { type: "json" };
import worlds from "../../src/wsorb/controller_worlds.json" with { type: "json" };

export type FlightSelection = { profileId: string; worldId: string; seed: number; duration: number };
export const flightWorlds = worlds;
export const flightProfile = (id: string) => profiles.find(profile => profile.id === (id === "wso_official_profile" ? "ali_official_profile" : id));

export function flightSelection(params: URLSearchParams): FlightSelection | null {
  if (["profile", "world", "seed", "duration"].some(key => params.getAll(key).length !== 1)) return null;
  const profile = flightProfile(params.get("profile")!);
  const world = worlds.find(world => world.id === params.get("world"));
  const seed = Number(params.get("seed")), duration = Number(params.get("duration"));
  if (!profile || !world || !/^\d+$/.test(params.get("seed")!) || !Number.isSafeInteger(seed) || seed < 0 || seed > 0xffffffff
    || !Number.isFinite(duration) || duration <= 0 || duration > 1800) return null;
  return { profileId: profile.id, worldId: world.id, seed, duration };
}

export function flightLink(profile: string, world: string, seed: number, duration: number): string | null {
  const params = new URLSearchParams({ profile, world, seed: String(seed), duration: String(duration) });
  const selected = flightSelection(params);
  if (!selected) return null;
  params.set("profile", selected.profileId);
  return `/?${params}`;
}
