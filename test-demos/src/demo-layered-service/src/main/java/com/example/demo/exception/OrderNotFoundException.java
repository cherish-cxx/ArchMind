package com.example.demo.exception;

public class OrderNotFoundException extends RuntimeException {

    public OrderNotFoundException(long id) {
        super("order not found: " + id);
    }
}
