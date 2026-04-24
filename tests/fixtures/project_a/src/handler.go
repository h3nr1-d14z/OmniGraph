package handler

import "project_a/src/model"

// GetUserHandler handles HTTP requests for user retrieval.
type GetUserHandler struct {
	repo *model.UserRepository
}

// Handle processes the request and returns a User.
func (h *GetUserHandler) Handle(id int64) (*model.User, error) {
	return h.repo.FindByID(id)
}
