InternalTokenFilter是一个普通的Bean，内部完成doFilterInternal过滤器
```java
    @Override  
    protected void doFilterInternal(HttpServletRequest request,  
                                    HttpServletResponse response,  
                                    FilterChain filterChain) throws ServletException, IOException {  
//        如果请求地址不是以指定的 "/internal/"开头直接方向,不做token检验  
//        该过滤器是局部拦截器,只负责保护该internal这一组接口,不是全局鉴权,不是所有接口都要校验这个内部token  
        if (!request.getRequestURI().startsWith(PREFIX)) {  
            filterChain.doFilter(request, response);  
            return;  
        }  
//从请求头HEADER字段取出HEADER  
        String token = request.getHeader(HEADER);  
        // 定长比较，避免逐字符短路带来的时序侧信道  
//        token不为空并且使用messageDigest.isEqual做字节数组对比  
        if (token != null  
                && MessageDigest.isEqual(expectedToken, token.getBytes(StandardCharsets.UTF_8))) {  
            filterChain.doFilter(request, response);  
            return;  
        }  
//      鉴权失败打印请求方法+请求URL  
        log.warn("内部接口鉴权失败: {} {}", request.getMethod(), request.getRequestURI());  
//        设置HTTP状态码:SC_UNAUTHORIZED==401  
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);  
//        设置响应类型为JSON  
        response.setContentType("application/json;charset=UTF-8");  
        objectMapper.writeValue(response.getOutputStream(),  
                Result.fail(ResultCode.UNAUTHORIZED.getCode(), "内部接口鉴权失败"));  
    }
```
作用：
不做保护范围的接口，本过滤器不做任何校验直接交给下一个过滤器链下一个Filter
只有命中前缀的内部接口，才强制校验这个内部Token

### 结合SecurityConfig拆解完成链路
SecurityConfig内部会在某个位置添加自定义过滤器
```java
// 6. 添加 JWT 过滤器（在 UsernamePasswordAuthenticationFilter 之前）  
.addFilterBefore(jwtAuthenticationFilter, UsernamePasswordAuthenticationFilter.class)  

110行

// 7. 内部接口令牌过滤器：只对 /internal/** 生效，其余路径直接放行  
.addFilterBefore(internalTokenFilter, UsernamePasswordAuthenticationFilter.class);
```
启动时把InternalTokenFilter实例放进过滤器链的指定位置，SecurityConfig只是配置描述，不会直接调用doFilterInternal（）方法，实际调用过滤器的是==🟠**Tomcat（Servlet 容器）→ DelegatingFilterProxy → Spring Security 过滤器链，依次执行每一个 Filter**==


---

时序：

1. 浏览器发 HTTP 请求 → 到达 Tomcat
2. Tomcat 触发 Servlet 的 Filter 机制，进入 Spring 的代理过滤器 `DelegatingFilterProxy`
3. `DelegatingFilterProxy` 拿到完整的 `SecurityFilterChain`（里面包含你注册的 InternalTokenFilter）
4. **循环遍历过滤器链，依次执行每个 Filter**
    
    - 轮到你的 InternalTokenFilter，调用 `doFilter()`
    - `OncePerRequestFilter` 的 `doFilter()` 父类方法内部，会调用你重写的 `doFilterInternal()`
    

> 调用关系：
> 
> Tomcat → DelegatingFilterProxy → 过滤器链表 → InternalTokenFilter.doFilter () → 父类 → doFilterInternal ()（你写的代码）


---

### 区分两个阶段

1. **启动阶段（一次性）**
    
    Spring 扫描 SecurityConfig → 执行 `securityFilterChain()` → 执行 `addFilterBefore` → 将你的自定义 Filter 实例加入过滤器链，存入 IoC。
    
    👉 执行者：Spring IoC 容器
    
2. **请求阶段（每次请求都会走）**
    
    HTTP 请求进来，Servlet 容器驱动过滤器链，执行每个 Filter 的过滤方法。
    
    👉 执行者：Servlet 容器 + Spring Security 的过滤器链执行器