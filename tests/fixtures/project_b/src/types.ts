export interface UserProfile {
  id: number;
  email: string;
  displayName: string;
}

export interface AuthState {
  token: string;
  user: UserProfile | null;
}
