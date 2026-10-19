package com.example.multi.api;

import java.util.List;

public interface UserService {

    User register(String username, String email, String rawPassword);

    User find(long id);

    List<User> list();
}
