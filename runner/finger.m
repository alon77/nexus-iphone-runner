// finger — one real finger drag on a booted simulator, built by the Simulator's own touch builder
// (SimulatorKit IndigoHIDMessageForMouseNSEvent, the path its mouse takes) and sent through SimDeviceLegacyHIDClient.
// A drag that starts on a screen edge carries that edge's flag, as the iPhone digitizer marks it, so iOS Safari
// turns a left-edge drag into its back gesture (chain iphone_real_edge_swipe_20261010 trap 3: 9 of 9 back).
// Usage: finger <udid> <none|top|left|bottom|right> <x0> <y0> <x1> <y1>   (x, y: shares 0..1 of the screen)
// Built on the runner by iphone_run.yml: clang -fobjc-arc -framework Foundation -framework CoreGraphics

#import <CoreGraphics/CoreGraphics.h>
#import <Foundation/Foundation.h>
#import <dlfcn.h>

static const uint32_t SCREEN_TARGET = 0x32;
static const NSUInteger TOUCH_DOWN = 1;
static const NSUInteger TOUCH_UP = 2;
static const NSUInteger TOUCH_DRAGGED = 6;
static const useconds_t HOLD_ON_EDGE_US = 150000;
static const useconds_t BETWEEN_DRAGS_US = 30000;
static const int DRAG_STEPS = 4;
static const int64_t ACK_WAIT_NS = 3 * NSEC_PER_SEC;
static const int ARGUMENT_COUNT = 7;
static NSString *const CORE_SIMULATOR = @"/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator";
static NSString *const SIMULATOR_KIT = @"Library/PrivateFrameworks/SimulatorKit.framework/SimulatorKit";

typedef void *(*touch_builder_fn)(CGPoint *, CGPoint *, uint32_t, NSUInteger, CGSize, uint32_t);

@protocol sim_service_context
- (id)sharedServiceContextForDeveloperDir:(NSString *)developer_dir error:(NSError **)error;
- (id)defaultDeviceSetWithError:(NSError **)error;
@end

@protocol sim_device_set
- (NSArray *)devices;
@end

@protocol sim_device
- (NSUUID *)UDID;
@end

@protocol legacy_hid_client
- (instancetype)initWithDevice:(id)device error:(NSError **)error;
- (void)sendWithMessage:(void *)message freeWhenDone:(BOOL)free_when_done completionQueue:(dispatch_queue_t)queue
             completion:(void (^)(NSError *))completion;
@end

static void fail(NSString *what) {
    fprintf(stderr, "finger: %s\n", what.UTF8String);
    exit(1);
}

static uint32_t edge_named(NSString *name) {
    NSArray *edges = @[@"none", @"top", @"left", @"bottom", @"right"];
    NSUInteger edge = [edges indexOfObject:name];
    if (edge == NSNotFound) fail([NSString stringWithFormat:@"edge %@ is not one of %@", name, [edges componentsJoinedByString:@", "]]);
    return (uint32_t)edge;
}

static NSString *developer_dir(void) {
    NSTask *task = [NSTask new];
    NSPipe *pipe = [NSPipe pipe];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/xcode-select"];
    task.arguments = @[@"-p"];
    task.standardOutput = pipe;
    [task launchAndReturnError:nil];
    [task waitUntilExit];
    NSData *output = [pipe.fileHandleForReading readDataToEndOfFile];
    return [[[NSString alloc] initWithData:output encoding:NSUTF8StringEncoding]
            stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
}

static id booted_device(NSString *developer, NSString *udid) {
    if (!dlopen(CORE_SIMULATOR.UTF8String, RTLD_NOW)) fail(@"CoreSimulator did not load");
    NSError *error = nil;
    id context = [(id<sim_service_context>)NSClassFromString(@"SimServiceContext") sharedServiceContextForDeveloperDir:developer error:&error];
    for (id device in [(id<sim_device_set>)[context defaultDeviceSetWithError:&error] devices]) {
        if ([[(id<sim_device>)device UDID].UUIDString isEqualToString:udid]) return device;
    }
    fail([NSString stringWithFormat:@"no simulator %@ (%@)", udid, error]);
    return nil;
}

static touch_builder_fn touch_builder(NSString *developer) {
    NSString *kit_path = [developer stringByAppendingPathComponent:SIMULATOR_KIT];
    void *kit = dlopen(kit_path.UTF8String, RTLD_NOW);
    if (!kit) fail([NSString stringWithFormat:@"SimulatorKit did not load from %@", kit_path]);
    touch_builder_fn builder = (touch_builder_fn)dlsym(kit, "IndigoHIDMessageForMouseNSEvent");
    if (!builder) fail(@"IndigoHIDMessageForMouseNSEvent is not in SimulatorKit");
    return builder;
}

static id hid_client(id device) {
    Class client_class = NSClassFromString(@"SimulatorKit.SimDeviceLegacyHIDClient") ?: NSClassFromString(@"SimDeviceLegacyHIDClient");
    if (!client_class) fail(@"SimDeviceLegacyHIDClient is not in SimulatorKit");
    NSError *error = nil;
    id client = [(id<legacy_hid_client>)[client_class alloc] initWithDevice:device error:&error];
    if (!client) fail([NSString stringWithFormat:@"HID client refused: %@", error]);
    return client;
}

static CGPoint point_at(CGPoint start, CGPoint end, int step) {
    return CGPointMake(start.x + (end.x - start.x) * step / DRAG_STEPS, start.y + (end.y - start.y) * step / DRAG_STEPS);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != ARGUMENT_COUNT) fail(@"usage: finger <udid> <none|top|left|bottom|right> <x0> <y0> <x1> <y1>");
        uint32_t edge = edge_named(@(argv[2]));
        CGPoint start = CGPointMake(atof(argv[3]), atof(argv[4]));
        CGPoint end = CGPointMake(atof(argv[5]), atof(argv[6]));
        NSString *developer = developer_dir();
        id device = booted_device(developer, @(argv[1]));
        touch_builder_fn builder = touch_builder(developer);
        id client = hid_client(device);
        dispatch_queue_t queue = dispatch_queue_create("finger", DISPATCH_QUEUE_SERIAL);
        dispatch_group_t acks = dispatch_group_create();
        __block NSError *refused = nil;
        void (^send)(CGPoint, NSUInteger) = ^(CGPoint point, NSUInteger phase) {
            void *message = builder(&point, NULL, SCREEN_TARGET, phase, CGSizeMake(1, 1), edge);
            if (!message) fail([NSString stringWithFormat:@"the touch builder dropped phase %lu (a drag under 16ms after the last touch)", (unsigned long)phase]);
            dispatch_group_enter(acks);
            [(id<legacy_hid_client>)client sendWithMessage:message freeWhenDone:YES completionQueue:queue completion:^(NSError *error) {
                if (error && !refused) refused = error;
                dispatch_group_leave(acks);
            }];
        };
        send(start, TOUCH_DOWN);
        usleep(HOLD_ON_EDGE_US);
        for (int step = 1; step <= DRAG_STEPS; step++) {
            send(point_at(start, end, step), TOUCH_DRAGGED);
            usleep(BETWEEN_DRAGS_US);
        }
        send(end, TOUCH_UP);
        if (dispatch_group_wait(acks, dispatch_time(DISPATCH_TIME_NOW, ACK_WAIT_NS))) fail(@"the simulator never acknowledged the touches");
        if (refused) fail([NSString stringWithFormat:@"the simulator refused a touch: %@", refused]);
    }
    return 0;
}
