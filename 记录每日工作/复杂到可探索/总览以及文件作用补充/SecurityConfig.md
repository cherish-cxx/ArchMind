SecurityConfig这个配置类，不是自己写代码new出来调用的，调用方是Spring框架。

注解@Configuration+@EnableWebSecurity目的是告诉Spring，这个是安全配置类，里面的Bean要交给Spring容器管理

###  谁发现这个类？

Spring Boot 启动时的**组件扫描（Component Scan）**

扫描到带 `@Configuration` 的类，就会解析这个类，把里面 `@Bean` 方法创建的对象注册到 Spring IoC 容器。
SecurityFilterChain（）这个方法不是手动调用，是Spring容器中初始化Bean的时候自动调用，生成SecurityFileterChain对象放进容器

### 真实使用这个Bean配置的是谁？

**Spring Security 底层的过滤器链相关组件（Filter、SecurityBuilder）**
- Springweb会注册一个核心过滤器：DelegatingFilterProxy
- 这个代理过滤器会从SpringIOC容器找到名字叫`springSecurityFilterChain` 的 Bean（就是你上面方法返回的 `SecurityFilterChain`）
- 当请求进来的时候，DelegatingFilterProxy委托这个SecurityFilterChain执行登录，鉴权，csrf，cors等安全逻辑

时序：

1. SpringBoot 启动 → 扫描 SecurityConfig
2. Spring 调用 `securityFilterChain()`，创建 SecurityFilterChain Bean 存入容器
3. Spring Security 内部 `DelegatingFilterProxy` 拿到这个 Bean
4. 浏览器发请求 → 进入过滤器链 → 执行你在 SecurityConfig 里定义的安全规则

### 3. 区分两个概念（很多人容易混淆）

1. **SecurityConfig 类本身**：配置类，作用是**定义 Bean**，只在容器初始化阶段被 Spring 解析、执行 @Bean 方法；请求来时不会再实例化 / 调用这个类。
2. **SecurityFilterChain Bean**：`@Bean`方法返回的实例，**请求进来的时候真正被使用**，由 Spring Security 过滤器调用。