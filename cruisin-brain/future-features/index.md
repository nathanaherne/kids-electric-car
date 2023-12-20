+++
title = "Future Features"
description = "An overview of cloud computing platforms and their benefits"
categories = [""]
tags = [""]

draft = false
author = "Nathan Aherne"

weight = 20
+++

### Upcoming Features

We are currently developing and testing the following features:

1. IMU ([AHRS](https://en.wikipedia.org/wiki/Attitude_and_heading_reference_system)) support for increased safety
	- speed reduction in turns (limit the lateral G forces the driver experiences in a turn)
	- crash detection and automatic vehicle disable
	- incline detection and rollover protection
2. Additional motor controller support
	- Support for 2 motor controllers (one for each rear wheel)
	- Support for the Spark Motor Controller
3. Electronic Differential
	- The electronic differential switches between locked (when driving straight) and open when turning.
	- Requires a motor for each wheel
	- Relies on IMU feature
4. OLED screen
	- To provide feedback on your cars vital information
5. Battery monitoring and feedback
	- Provide remaining range information
	- To protect the battery from being over discharged
	- To protect the motor controller from over current conditions
6. Input and Output mapping
	- To allow you to configure the manual control inputs the way you would like
7. Lighting options
	- Electrically activated headlights
	- Automatically activated brake lights
	- Indicators and hazard lights

### Future Features

These features will be added to the Cruisin Brain once the above features have been developed and tested.

1. GPS 
	- to allow the Cruisin Brain to provide detailed range information
	- to extend existing safety features with GPS information
2. Wifi/Bluetooth programming and control
	- Allow the Cruisin Brain to be configured from your phone.
	- Allow the Cruisin Electric Car to be remote controlled from your phone.
3. Additional motor controller support
	- Support for the [VESC motor controller](https://vesc-project.com/)
	- Multiple (up to 4) motor controllers on a single car (4WD)
4. Remote Control
	- To allow cars to be remotely controlled by parents or carers
	- This feature is being developed for disabled and very young drivers.
	- This feature is also being developed for autonomous applications of the Cruisin Electric Car
	- This feature somewhat depends on the power steering feature
5. Power Steering
	- To aid drivers who are unable to steer the car
	- To provide 4 wheel steering for increased maneuverability
	- This feature is being developed for disabled and very young drivers.
	- This feature is also being developed for robotic applications of the Cruisin Electric Car
6. Drive by wire
	- Allows a driver to move a steering wheel (unattached to the steering column) or joystick and have all actions (steering, lights, braking) electrically activated.
	- This feature is being developed for disabled and very young drivers.
	- This feature depends on the Power Steering feature