import { UserProfile } from "./types";

export async function fetchUser(id: number): Promise<UserProfile> {
  const res = await fetch(`/api/users/${id}`);
  return res.json();
}
