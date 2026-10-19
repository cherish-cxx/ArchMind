package com.example.multi.app;

import com.example.multi.api.User;
import com.example.multi.api.UserService;

import java.util.List;

public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    public User register(String username, String email, String rawPassword) {
        return userService.register(username, email, rawPassword);
    }

    public User find(long id) {
        return userService.find(id);
    }

    public List<User> list() {
        return userService.list();
    }
}
