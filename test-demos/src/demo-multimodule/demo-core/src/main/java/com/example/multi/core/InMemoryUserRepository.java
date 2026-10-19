package com.example.multi.core;

import com.example.multi.api.User;
import com.example.multi.api.UserRepository;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

public class InMemoryUserRepository implements UserRepository {

    private final Map<Long, User> store = new ConcurrentHashMap<>();
    private final AtomicLong sequence = new AtomicLong(1000);

    @Override
    public User save(User user) {
        long id = user.getId() > 0 ? user.getId() : sequence.incrementAndGet();
        User stored = new User(id, user.getUsername(), user.getEmail(), user.getPasswordHash());
        store.put(id, stored);
        return stored;
    }

    @Override
    public Optional<User> findById(long id) {
        return Optional.ofNullable(store.get(id));
    }

    @Override
    public List<User> findAll() {
        return new ArrayList<>(store.values());
    }
}
