package model

// UserRepository provides data access for User.
type UserRepository struct{}

func (r *UserRepository) FindByID(id int64) (*User, error) {
	return &User{ID: id, Email: "test@example.com"}, nil
}
