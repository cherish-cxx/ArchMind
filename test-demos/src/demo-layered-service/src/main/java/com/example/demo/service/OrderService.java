package com.example.demo.service;

import com.example.demo.model.Order;
import com.example.demo.model.OrderRequest;

import java.util.List;

public interface OrderService {

    Order create(OrderRequest request);

    Order get(long id);

    List<Order> list();
}
