package com.example.multi.app;

import com.example.multi.api.User;
import com.example.multi.api.UserRepository;
import com.example.multi.api.UserService;
import com.example.multi.core.InMemoryUserRepository;
import com.example.multi.core.PasswordEncoder;
import com.example.multi.core.UserServiceImpl;

public class AppMain {

    public static void main(String[] args) {
        UserRepository repository = new InMemoryUserRepository();
        PasswordEncoder encoder = new PasswordEncoder();
        UserService userService = new UserServiceImpl(repository, encoder);
        UserController controller = new UserController(userService);

        User alice = controller.register("alice", "alice@example.com", "secret");
        User bob = controller.register("bob", "bob@example.com", "secret");

        System.out.println("alice id=" + alice.getId() + " hash=" + alice.getPasswordHash().substring(0, 8));
        System.out.println("bob id=" + bob.getId());
        System.out.println("total users=" + controller.list().size());
    }
}
