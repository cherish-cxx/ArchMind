package com.example.multi.core;

import com.example.multi.api.User;
import com.example.multi.api.UserRepository;
import com.example.multi.api.UserService;

import java.util.List;

public class UserServiceImpl implements UserService {

    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;

    public UserServiceImpl(UserRepository userRepository, PasswordEncoder passwordEncoder) {
        this.userRepository = userRepository;
        this.passwordEncoder = passwordEncoder;
    }

    @Override
    public User register(String username, String email, String rawPassword) {
        boolean exists = userRepository.findAll().stream()
                .anyMatch(user -> user.getUsername().equals(username));
        if (exists) {
            throw new IllegalArgumentException("username already exists: " + username);
        }
        String hash = passwordEncoder.encode(rawPassword);
        return userRepository.save(new User(0, username, email, hash));
    }

    @Override
    public User find(long id) {
        return userRepository.findById(id)
                .orElseThrow(() -> new IllegalArgumentException("user not found: " + id));
    }

    @Override
    public List<User> list() {
        return userRepository.findAll();
    }
}
