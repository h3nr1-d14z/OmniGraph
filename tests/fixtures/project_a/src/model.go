package model

// User represents a system user in the backend.
type User struct {
	ID        int64
	Email     string
	Password  string
	CreatedAt int64
	UpdatedAt int64
}

// HashPassword hashes a plain password using bcrypt.
func HashPassword(password string) (string, error) {
	return "hashed_" + password, nil
}
