// SPDX-License-Identifier: AGPL-3.0-only
// macOS headless adapter for Bambu Studio's unmodified networking plugin.
// The public ABI is pinned to Studio v02.08.02.61 / plugin 02.08.02.x.
// No GUI or app-binary modification. The launcher loads a process-local
// executable-path shim into this helper, not into Bambu Studio.
#import <Foundation/Foundation.h>
#include <dlfcn.h>
#include <unistd.h>
#include <fcntl.h>
#include <atomic>
#include <chrono>
#include <thread>
#include <mutex>
#include "vendor/bambu_networking.hpp"

static int result_fd=-1;
static std::mutex output_mutex;
static void emit(NSDictionary *value) {
    @autoreleasepool {
        NSData *data=[NSJSONSerialization dataWithJSONObject:value options:0 error:nil];
        if(!data)return;
        std::lock_guard<std::mutex> lock(output_mutex);
        write(result_fd,data.bytes,data.length);write(result_fd,"\n",1);
    }
}
template<class T> static T sym(void *lib,const char *name) {
    void *p=dlsym(lib,name);
    if(!p){emit(@{@"event":@"error",@"message":@"Required plugin symbol missing"});_exit(3);}
    return reinterpret_cast<T>(p);
}
static std::string str(NSDictionary *d,NSString *key) {
    id v=d[key];return [v isKindOfClass:NSString.class]?std::string([v UTF8String]):std::string();
}
static NSDictionary *parse(const std::string &s) {
    NSData *d=[NSData dataWithBytes:s.data() length:s.size()];
    id j=[NSJSONSerialization JSONObjectWithData:d options:0 error:nil];
    return [j isKindOfClass:NSDictionary.class]?j:@{};
}
static NSDictionary *pick(NSDictionary *d,NSArray *keys) {
    NSMutableDictionary *safe=[NSMutableDictionary dictionary];
    for(NSString *k in keys)if(d[k])safe[k]=d[k];
    return safe;
}
int main(){@autoreleasepool {
    // Suppress all vendor logging before loading the plugin. Only explicit,
    // allowlisted JSON events use the saved output descriptor.
    result_fd=dup(STDOUT_FILENO);
    int nullfd=open("/dev/null",O_WRONLY);dup2(nullfd,1);dup2(nullfd,2);close(nullfd);
    NSData *input=[[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
    NSDictionary *cfg=[NSJSONSerialization JSONObjectWithData:input options:0 error:nil];
    if(![cfg isKindOfClass:NSDictionary.class])_exit(3);
    std::string action=str(cfg,@"action"), serial=str(cfg,@"serial"), ip=str(cfg,@"ip");
    if(action!="status"&&action!="sign_probe"&&action!="start"&&action!="control")_exit(3);
    const std::string control=str(cfg,@"control");
    if(action=="control"&&control!="pause"&&control!="resume"&&control!="stop")_exit(3);
    void *lib=dlopen(str(cfg,@"plugin").c_str(),RTLD_NOW|RTLD_LOCAL);
    if(!lib){emit(@{@"event":@"error",@"message":@"Plugin could not be loaded"});_exit(3);}
    auto version=sym<std::string(*)()>(lib,"bambu_network_get_version")();
    emit(@{@"event":@"plugin",@"version":[NSString stringWithUTF8String:version.c_str()]});
    if(version.substr(0,8)!="02.08.02"){
        emit(@{@"event":@"error",@"message":@"Plugin ABI changed; update the pinned adapter before printing"});_exit(3);
    }
    void *agent=sym<void*(*)(std::string)>(lib,"bambu_network_create_agent")(str(cfg,@"scratch"));
    if(!agent)_exit(3);
    sym<int(*)(void*,std::string)>(lib,"bambu_network_set_config_dir")(agent,str(cfg,@"config_dir"));
    sym<int(*)(void*,std::string,std::string)>(lib,"bambu_network_set_cert_file")(agent,str(cfg,@"cert_dir"),"slicer_base64.cer");
    sym<int(*)(void*,std::string)>(lib,"bambu_network_set_country_code")(agent,str(cfg,@"country"));
    std::atomic<bool> connected{false},got_status{false},got_start{false},got_probe{false},rejected{false};
    std::atomic<bool> submitted{false},cert_ready{false};
    std::atomic<bool> control_ack{false},control_state{false};
    std::mutex state_mutex;
    std::string current_state,current_job;
    long long current_error=0;
    bool error_known=false;
    const std::string expected=str(cfg,@"job_name"),sequence=str(cfg,@"sequence_id");
    BBL::OnLocalConnectedFn conn=[&](int status,std::string,std::string){if(status==0)connected=true;};
    BBL::OnPrinterConnectedFn pc=[&](std::string){connected=true;};
    BBL::OnMessageFn onmsg=[&](std::string,std::string message){@autoreleasepool {
        if(message=="device_cert_installed"){cert_ready=true;emit(@{@"event":@"device_certificate_ready"});return;}
        NSDictionary *root=parse(message),*p=root[@"print"];
        NSDictionary *security=root[@"security"];
        if([security isKindOfClass:NSDictionary.class]) {
            NSMutableDictionary *safe=[pick(security,@[@"command",@"result",@"err_code",@"reason"]) mutableCopy];safe[@"event"]=@"certificate_reply";emit(safe);
            if(str(security,@"command")=="app_cert_install"&&str(security,@"result")=="SUCCESS")cert_ready=true;
        }
        if(![p isKindOfClass:NSDictionary.class])return;
        NSMutableDictionary *safe=[pick(p,@[@"command",@"sequence_id",@"gcode_state",@"subtask_name",@"stg_cur",@"mc_percent",@"layer_num",@"total_layer_num",@"mc_remaining_time",@"print_error",@"result",@"reason",@"err_code",@"bed_temper",@"bed_target_temper",@"nozzle_temper",@"nozzle_target_temper"]) mutableCopy];
        safe[@"event"]=@"telemetry";
        if(p[@"ams"]){
            NSMutableArray *trays=[NSMutableArray array];
            for(NSDictionary *a in p[@"ams"][@"ams"]){
                for(NSDictionary *t in a[@"tray"]){
                    NSMutableDictionary *tray=[pick(t,@[@"id",@"tray_type",@"tray_color",@"tray_info_idx"]) mutableCopy];
                    tray[@"ams_id"]=a[@"id"]?:@"";[trays addObject:tray];
                }
            }
            safe[@"trays"]=trays;
        }
        if(p[@"device"][@"nozzle"]){
            NSMutableArray *nozzles=[NSMutableArray array];
            for(NSDictionary *n in p[@"device"][@"nozzle"][@"info"])
                [nozzles addObject:pick(n,@[@"id",@"diameter",@"type"])];
            safe[@"nozzles"]=nozzles;
        }
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            if(p[@"gcode_state"]){current_state=str(p,@"gcode_state");got_status=true;}
            if(p[@"subtask_name"])current_job=str(p,@"subtask_name");
            if(p[@"print_error"]){current_error=[p[@"print_error"] longLongValue];error_known=true;}
            if(submitted && current_job==expected && (current_state=="PREPARE"||current_state=="RUNNING"))got_start=true;
            if(action=="control"&&submitted&&p[@"gcode_state"]){
                bool same_job=current_job==expected;
                bool stopped=control=="stop"&&(current_state=="IDLE"||current_state=="FAILED")&&(same_job||current_job.empty());
                control_state=stopped||(same_job&&((control=="pause"&&current_state=="PAUSE")||(control=="resume"&&current_state=="RUNNING")));
            }
        }
        if(str(p,@"command")=="extrusion_cali_get" && str(p,@"sequence_id")==sequence){
            got_probe=true;
            if([p[@"err_code"] longLongValue]!=0)rejected=true;
        }
        if(str(p,@"command")=="project_file" && str(p,@"sequence_id")==sequence){
            if([p[@"err_code"] longLongValue]!=0 || str(p,@"result")=="FAIL" || str(p,@"result")=="fail")rejected=true;
        }
        if(action=="control"&&submitted&&str(p,@"command")==control&&str(p,@"sequence_id")==sequence){
            const auto result=str(p,@"result");
            if([p[@"err_code"] longLongValue]!=0||result=="FAIL"||result=="fail")rejected=true;
            else if(result=="SUCCESS"||result=="success")control_ack=true;
        }
        emit(safe);
    }};
    sym<int(*)(void*,BBL::OnLocalConnectedFn)>(lib,"bambu_network_set_on_local_connect_fn")(agent,conn);
    sym<int(*)(void*,BBL::OnPrinterConnectedFn)>(lib,"bambu_network_set_on_printer_connected_fn")(agent,pc);
    sym<int(*)(void*,BBL::OnMessageFn)>(lib,"bambu_network_set_on_local_message_fn")(agent,onmsg);
    sym<int(*)(void*,BBL::OnMessageFn)>(lib,"bambu_network_set_on_message_fn")(agent,onmsg);
    int rc=sym<int(*)(void*)>(lib,"bambu_network_start")(agent);
    if(rc!=0){emit(@{@"event":@"error",@"message":@"Plugin startup failed",@"rc":@(rc)});_exit(4);}
    rc=sym<int(*)(void*,std::string,std::string,std::string,std::string,bool)>(lib,"bambu_network_connect_printer")(agent,serial,ip,"bblp",str(cfg,@"access_code"),true);
    if(rc!=0)_exit(4);
    for(int i=0;i<150&&!connected;i++)std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if(!connected){emit(@{@"event":@"error",@"message":@"Printer connection timed out"});_exit(4);}
    auto send=sym<int(*)(void*,std::string,std::string,int,int)>(lib,"bambu_network_send_message_to_printer");
    send(agent,serial,R"({"pushing":{"sequence_id":"24999","command":"pushall","version":1,"push_target":1}})",1,0);
    for(int i=0;i<120&&!got_status;i++)std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if(!got_status){emit(@{@"event":@"error",@"message":@"No fresh printer status"});_exit(4);}
    if(action=="status"){
        emit(@{@"event":@"status_complete"});
        sym<int(*)(void*)>(lib,"bambu_network_disconnect_printer")(agent);_exit(0);
    }
    // Required lifecycle: initialization alone does not populate the device's
    // signing/encryption state. Use the same exported certificate APIs as Studio.
    rc=sym<int(*)(void*)>(lib,"bambu_network_update_cert")(agent);
    emit(@{@"event":@"certificate_refresh",@"rc":@(rc)});
    // The boolean selects the existing LAN transport for this certificate
    // exchange. It does not toggle the printer's LAN Only setting.
    sym<void(*)(void*,std::string,bool)>(lib,"bambu_network_install_device_cert")(agent,serial,true);
    for(int i=0;i<100&&!cert_ready;i++)std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if(!cert_ready){emit(@{@"event":@"error",@"message":@"Device certificate exchange not confirmed"});_exit(4);}
    std::string payload;
    if(action=="sign_probe"){
        payload="{\"print\":{\"command\":\"extrusion_cali_get\",\"sequence_id\":\""+sequence+"\",\"filament_id\":\"GFL99\",\"nozzle_diameter\":\"0.4\"}}";
    }else if(action=="control"){
        payload=str(cfg,@"payload");
        NSDictionary *p=parse(payload)[@"print"];
        if(expected.empty()||str(p,@"command")!=control||str(p,@"sequence_id")!=sequence||str(p,@"param")!="")_exit(3);
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            bool allowed=(control=="pause"&&current_state=="RUNNING")||
                (control=="resume"&&current_state=="PAUSE"&&error_known&&current_error==0)||
                (control=="stop"&&(current_state=="PREPARE"||current_state=="RUNNING"||current_state=="PAUSE"));
            if(current_job!=expected||!allowed){
                emit(@{@"event":@"error",@"message":@"Job or state changed; control refused"});_exit(5);
            }
            submitted=true;
        }
        emit(@{@"event":@"dispatching"});
    }else{
        {
            std::lock_guard<std::mutex> lock(state_mutex);
            if(current_state!="IDLE"&&current_state!="FINISH"&&current_state!="FAILED"){
                emit(@{@"event":@"error",@"message":@"Printer is busy; start refused"});_exit(5);
            }
        }
        payload=str(cfg,@"payload");
        NSDictionary *p=parse(payload)[@"print"];
        if(str(p,@"command")!="project_file"||str(p,@"sequence_id")!=sequence||str(p,@"subtask_name")!=expected)_exit(3);
        submitted=true;
        emit(@{@"event":@"dispatching"});
    }
    rc=send(agent,serial,payload,1,0);
    emit(@{@"event":@"send_result",@"rc":@(rc)});
    if(rc!=0)_exit(6);
    auto complete=[&](){return action=="control"?(control_ack.load()&&control_state.load()):(action=="start"?got_start.load():got_probe.load());};
    for(int i=0;i<450&&!rejected&&!complete();i++)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    bool confirmed=complete();
    emit(@{@"event":@"verification",@"confirmed":@(confirmed&&!rejected),@"rejected":@(rejected.load())});
    sym<int(*)(void*)>(lib,"bambu_network_disconnect_printer")(agent);
    // Vendor worker teardown may block. Disconnect first; process exit closes
    // remaining sockets and destroys all in-memory credentials deterministically.
    _exit(confirmed&&!rejected?0:7);
}}
